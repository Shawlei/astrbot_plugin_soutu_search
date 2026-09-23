"""Yandex（yandex.ru）以图搜图反查 provider。

用途：全网多源以图搜图，对画师作品、Twitter、Pixiv、Booru 搬运站以及跨平台转载
收录极快、召回率极高。不需要任何 API Key。

接口说明：
- 默认端点使用 ``https://yandex.ru``（相较 yandex.com，对反爬与区域限制更友好）。
- 发送方式：POST 到 ``/images/search?rpt=imageview&format=json&request={...}``
  multipart 表单必须包含明确的 ``Content-Length``（预先拼接 bytes 避免 chunked 触发 413）。
- 响应：直接从服务端渲染（SSR）的 ``data-state`` 中提取 ``cbirSites.sites``（引用该图片的网页）
  以及 ``cbirSimilar.thumbs``（相似图）。
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.parse
from typing import Any

import aiohttp

from astrbot.api import logger

from .formatter import SearchResult, SourceOutcome

DEFAULT_BASE_URL = "https://yandex.ru"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 用于剥离 yandex 自身的跟踪参数
_UTM_PREFIX = "utm_"


def clean_url(raw_url: str) -> str:
    """清理 URL 中的追踪参数（如 utm_*），保留纯净页面地址。"""
    if not raw_url:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw_url)
        q = urllib.parse.parse_qsl(parsed.query)
        clean_q = [(k, v) for k, v in q if not k.lower().startswith(_UTM_PREFIX)]
        new_query = urllib.parse.urlencode(clean_q)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        return raw_url


def parse_yandex_html(html_text: str, base_url: str = DEFAULT_BASE_URL, top_k: int = 5) -> list[SearchResult]:
    """从 Yandex 搜索结果页 HTML 中提取匹配结果。"""
    if not html_text:
        return []

    states = re.findall(r'data-state="([^"]{300,})"', html_text)
    init_state = None
    for raw in states:
        try:
            obj = json.loads(html.unescape(raw))
            if isinstance(obj, dict) and "initialState" in obj:
                init_state = obj["initialState"]
                break
        except Exception:
            continue

    if not isinstance(init_state, dict):
        return []

    results: list[SearchResult] = []
    seen_urls: set[str] = set()

    # 1. 优先提取精确引用该图的网站 (cbirSites)
    sites = init_state.get("cbirSites", {}).get("sites", [])
    if isinstance(sites, list):
        for s in sites:
            if not isinstance(s, dict):
                continue
            raw_url = s.get("url") or ""
            url = clean_url(raw_url)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            title = (s.get("title") or s.get("description") or "未知标题").strip()
            domain = s.get("domain") or "网页来源"
            thumb_obj = s.get("thumb") or {}
            thumb_url = thumb_obj.get("url") or ""
            if thumb_url.startswith("//"):
                thumb_url = "https:" + thumb_url

            results.append(
                SearchResult(
                    title=title,
                    source=domain,
                    url=url,
                    thumbnail=thumb_url or None,
                    score=None,
                )
            )
            if len(results) >= top_k:
                return results

    # 2. 若 sites 为空，兜底取相似图片 (cbirSimilar)
    if not results:
        thumbs = init_state.get("cbirSimilar", {}).get("thumbs", [])
        if isinstance(thumbs, list):
            for t in thumbs:
                if not isinstance(t, dict):
                    continue
                raw_url = t.get("linkUrl") or t.get("imageUrl") or ""
                if raw_url.startswith("/"):
                    raw_url = base_url.rstrip("/") + raw_url
                url = clean_url(raw_url)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                title = (t.get("title") or "相似图片").strip()
                results.append(
                    SearchResult(
                        title=title,
                        source="yandex-similar",
                        url=url,
                        thumbnail=None,
                        score=None,
                    )
                )
                if len(results) >= top_k:
                    break

    return results


class YandexClient:
    """Yandex 识图反查客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
        top_k: int = 5,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = max(5, int(timeout))
        self.top_k = max(1, min(20, int(top_k)))
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            client_timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=client_timeout)
        return self._session

    async def search(self, image: bytes, filename: str = "image.jpg", mime: str = "image/jpeg") -> SourceOutcome:
        """上传图片并反查出处。"""
        if not image:
            raise ValueError("图片数据为空")

        session = await self._get_session()
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        part1 = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="prg"\r\n\r\n'
            f"1\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="upfile"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        part2 = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = part1 + image + part2

        params = {
            "rpt": "imageview",
            "format": "json",
            "request": '{"blocks":[{"block":"b-page_type_search-by-image__link"}]}',
        }
        url = f"{self.base_url}/images/search"
        headers = {
            "User-Agent": DEFAULT_UA,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "text/html,application/json,*/*",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/images/",
        }

        try:
            async with session.post(url, params=params, data=body, headers=headers) as resp:
                if resp.status != 200:
                    body_sample = (await resp.text())[:120].strip()
                    raise RuntimeError(f"Yandex 接口返回 HTTP {resp.status}: {body_sample}")

                resp_text = await resp.text()

                # 大多数情况下服务端直接在当前页返回带 SSR 数据的 HTML
                if "<html" in resp_text.lower():
                    results = parse_yandex_html(resp_text, base_url=self.base_url, top_k=self.top_k)
                else:
                    parsed = json.loads(resp_text)
                    cbir_id = parsed.get("blocks", [{}])[0].get("params", {}).get("cbirId")
                    if not cbir_id:
                        raise RuntimeError("未能从 Yandex 响应中获取 cbirId")
                    get_url = f"{self.base_url}/images/search?rpt=imageview&cbir_id={urllib.parse.quote(cbir_id)}&cbir_page=sites"
                    async with session.get(get_url, headers=headers) as get_resp:
                        if get_resp.status != 200:
                            raise RuntimeError(f"Yandex 获取结果页失败 HTTP {get_resp.status}")
                        html_text = await get_resp.text()
                        results = parse_yandex_html(html_text, base_url=self.base_url, top_k=self.top_k)

        except asyncio.TimeoutError as exc:
            logger.warning("[搜图] Yandex 请求超时 (%ss)", self.timeout)
            raise TimeoutError(f"Yandex 请求超时（{self.timeout}s），请稍后重试") from exc
        except aiohttp.ClientError as exc:
            logger.warning("[搜图] Yandex 网络请求失败: %s", exc)
            raise RuntimeError(f"Yandex 网络请求失败: {exc}") from exc

        return SourceOutcome(results=results)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
