"""ascii2d（ascii2d.net）以图反查 provider。

用途：把一张图片反查到**画师首发站**的原始出处（Pixiv / Twitter / Fanbox / Fantia /
Misskey / ニコニコ静画 / ニジエ）。与 SauceNAO 互补 —— SauceNAO 的 Twitter 索引早已
停更（2019-12），而 ascii2d 的数据库基本就是 **Twitter + Pixiv**，对**近期图**命中率更高。

接口（依据 ``recon/API_ASCII2D.md`` 规范，纯按规范实现，本机 curl 不可达）::

    POST {base_url}/search/file          # multipart 上传本地图片，字段名 file
    headers: 浏览器 UA + Referer: {base_url}/ + Origin: {base_url} + Accept-Language: ja,en;q=0.8

两种检索模式（``bovw``）：
- color（默认）：先 ``POST /search/file`` 拿到 color 结果页（URL 形如 ``.../color/...``）；
- bovw（特征检索）：再把结果页 URL 里的 ``/color/`` 换成 ``/bovw/`` **GET 一次**（故两次请求）。
  对**裁剪过 / 旋转过 / 色调不同**的图更有效，但更慢。

.. warning::
   **本机（开发环境）连不通 ascii2d.net（curl 出口被拦）**，故本模块的请求规范与 HTML 解析
   均来自**开源参考实现的选择器**，**未在本机做真实联网端到端验证**。真实连通性请在用户机器上
   用 ``tests/live_ascii2d_check.py`` 复验（解析结果为 0 条时会自动落盘原始 HTML 到
   ``recon/ascii2d_last.html`` 供排障）。

解析容错要点（均为线上真实会踩的坑）：
- **必须用 stdlib ``html.parser``**（不引入 bs4/lxml/pyquery 等新依赖）；
- 结果为空 / 缺 ``div.row.item-box`` / 缺 ``img`` / ``small`` / 链接 / 相对路径 → 全部优雅降级；
- ``<small>`` 来源标记大小写不敏感（``pixiv`` / ``Pixiv`` 均识别）；
- 标题含「詳細掲示板のログ」「2ちゃんねるのログ」等 2ch 噪声 → 置空；
- **不返回相似度分数**：统一模型 ``score`` 置 ``None``，**绝不**套用 SauceNAO 的 ``min_similarity``
  过滤（否则结果会被全滤掉）；按结果页**原始顺序**取前若干条。
"""

from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import aiohttp

from astrbot.api import logger

from .formatter import SearchResult, SourceOutcome

DEFAULT_BASE_URL = "https://ascii2d.net"

# 详情区 <small> 文本恰等于其一者即为「来源标记」（大小写不敏感）
SUPPORTED_SOURCES: tuple[str, ...] = (
    "fanbox",
    "fantia",
    "misskey",
    "pixiv",
    "twitter",
    "ニコニコ静画",
    "ニジエ",
)

# 来源标记 -> 展示名（中文 / 通用）
SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "pixiv": "Pixiv",
    "twitter": "Twitter/X",
    "fanbox": "Fanbox",
    "fantia": "Fantia",
    "misskey": "Misskey",
    "ニコニコ静画": "NicoNico静画",
    "ニジエ": "Nijie",
}

# 未知来源（无来源标记）时的展示名
UNKNOWN_SOURCE = "ascii2d"

# 标题噪声（2ch 日志），命中则置空
TITLE_NOISE_MARKERS: tuple[str, ...] = ("詳細掲示板のログ", "2ちゃんねるのログ")

# HTML 中的 void 元素：不参与标签栈（避免自闭合导致栈错位）
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

_WS_RE = re.compile(r"\s+")


def _clean(text: Any) -> str:
    """折叠空白并去掉首尾空白；非字符串返回空串。"""
    if text is None:
        return ""
    return _WS_RE.sub(" ", str(text)).strip()


def _match_mark(text: str) -> str:
    """把候选文本匹配到来源标记；命中返回**规范名**（SUPPORTED_SOURCES 中的写法），否则空串。

    大小写不敏感（拉丁字母标记如 ``Pixiv`` / ``TWITTER`` 均归一化）。
    """
    candidate = _clean(text)
    if not candidate:
        return ""
    lowered = candidate.lower()
    for source in SUPPORTED_SOURCES:
        if lowered == source.lower():
            return source
    return ""


def _abs_url(base_url: str, href: str) -> str:
    """把可能是相对路径的链接补全为绝对 URL；空串原样返回。"""
    value = _clean(href)
    if not value:
        return ""
    if value.lower().startswith(("http://", "https://")):
        return value
    base = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    try:
        return urljoin(base + "/", value)
    except Exception:  # noqa: BLE001 - 极端的非法 URL 不崩
        return value


# --------------------------------------------------------------------------- #
# HTML 解析（stdlib html.parser；纯函数、绝不抛异常）
# --------------------------------------------------------------------------- #
def _new_item() -> dict:
    """新建一个「结果条目」的原始累积容器。"""
    return {
        "hash": "",
        "smalls": [],
        "img": "",
        "h6": "",
        "external": "",
        "_links": [],
    }


class _Ascii2dHTMLParser(HTMLParser):
    """抽取 ascii2d 结果页中每个 ``div.row.item-box`` 的字段。

    采用**单遍扫描 + 标签栈**：遇到 class 含 ``item-box`` 的 ``div`` 即开新条目，栈回到该 div
    之下即收尾。文本按「最内层捕获帧（small / a / h6 / hash / external）」归属；同时把文本
    回灌给所有仍开放的 ``<small>`` 帧（处理 ``<small><a>…</a></small>`` 包裹）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._tag_stack: list[tuple[str, set, dict | None]] = []
        self._caps: list[dict] = []
        self._cur: dict | None = None
        self._item_level: int | None = None
        self._gray_depth = 0
        self._pending_a: dict | None = None

    # ---------------- 属性工具 ----------------
    @staticmethod
    def _class_set(attrs) -> set:
        for name, value in attrs or []:
            if name and name.lower() == "class" and value:
                return {c for c in str(value).split() if c}
        return set()

    @staticmethod
    def _attrs_map(attrs) -> dict:
        out: dict[str, str] = {}
        for name, value in attrs or []:
            if name:
                out[name.lower()] = value if value is not None else ""
        return out

    # ---------------- 标签处理 ----------------
    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        classes = self._class_set(attrs)
        attrs_map = self._attrs_map(attrs)

        # 新结果条目
        if tag == "div" and "item-box" in classes:
            self._finish_item()
            self._cur = _new_item()
            self._tag_stack.append((tag, classes, None))
            self._item_level = len(self._tag_stack)
            if "gray-link" in classes and self._gray_depth >= 0:
                self._gray_depth += 1
            return

        cap_kind: str | None = None
        link: dict | None = None
        if self._cur is not None:
            if tag == "img" and not self._cur["img"]:
                src = _clean(attrs_map.get("src"))
                if src:
                    self._cur["img"] = src
            if tag == "small":
                cap_kind = "small"
            elif tag == "a":
                link = {
                    "href": _clean(attrs_map.get("href")),
                    "text": "",
                    "gray": self._gray_depth > 0,
                    "pull": "pull-xs-right" in classes,
                }
                self._cur["_links"].append(link)
                cap_kind = "a"
            elif tag == "h6":
                cap_kind = "h6"
            elif "hash" in classes:
                cap_kind = "hash"
            elif "external" in classes:
                cap_kind = "external"

        if tag in _VOID_TAGS:
            return

        frame: dict | None = None
        if cap_kind:
            frame = {"kind": cap_kind, "buf": []}
            if cap_kind == "a":
                frame["link"] = link
        self._tag_stack.append((tag, classes, frame))
        if frame is not None:
            self._caps.append(frame)
        if "gray-link" in classes:
            self._gray_depth += 1

    def handle_startendtag(self, tag, attrs):
        # 自闭合标签：作为起始处理即可（void 不压栈；非 void 由配套 endtag 收尾）
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in _VOID_TAGS:
            return
        if not self._tag_stack:
            return
        _open_tag, classes, frame = self._tag_stack.pop()
        if frame is not None:
            if self._caps and self._caps[-1] is frame:
                self._caps.pop()
            elif frame in self._caps:
                self._caps.remove(frame)
            self._commit_frame(frame)
        if "gray-link" in classes and self._gray_depth > 0:
            self._gray_depth -= 1
        if self._item_level is not None and len(self._tag_stack) < self._item_level:
            self._finish_item()

    def handle_data(self, data):
        if not data or not self._caps:
            return
        innermost = self._caps[-1]
        for frame in self._caps:
            if frame is innermost or frame["kind"] == "small":
                frame["buf"].append(data)

    def _commit_frame(self, frame: dict) -> None:
        if self._cur is None:
            return
        text = _clean("".join(frame.get("buf", [])))
        kind = frame.get("kind")
        if kind == "small":
            self._cur["smalls"].append(text)
        elif kind == "a":
            link = frame.get("link")
            if isinstance(link, dict):
                link["text"] = text
        elif kind == "h6":
            if not self._cur["h6"]:
                self._cur["h6"] = text
        elif kind == "external":
            if not self._cur["external"]:
                self._cur["external"] = text
        elif kind == "hash":
            if not self._cur["hash"]:
                self._cur["hash"] = text

    def _finish_item(self) -> None:
        if self._cur is not None:
            self.items.append(self._cur)
        self._cur = None
        self._caps.clear()
        self._pending_a = None
        self._gray_depth = 0
        self._item_level = None


def _finalize_item(raw: dict, base_url: str) -> dict:
    """把原始累积容器加工为规范条目（标题/链接/画师/来源标记/缩略图）。"""
    smalls = [s for s in raw.get("smalls", []) if s]
    mark = ""
    detail = ""
    for text in smalls:
        matched = _match_mark(text)
        if matched and not mark:
            mark = matched
            continue
        if not detail and not matched:
            detail = text

    all_links = raw.get("_links", [])
    gray_links = [link for link in all_links if link.get("gray")]
    chosen = gray_links or all_links
    chosen = [link for link in chosen if link.get("href") or link.get("text")]

    title = ""
    url = ""
    author = ""
    author_url = ""
    if mark and len(chosen) >= 2:
        title = chosen[0].get("text", "")
        url = chosen[0].get("href", "")
        author = chosen[1].get("text", "")
        author_url = chosen[1].get("href", "")
    else:
        title = raw.get("h6") or raw.get("external") or ""
        if not title and chosen:
            title = chosen[0].get("text", "")
        if chosen:
            url = chosen[0].get("href", "")
        if len(chosen) >= 2:
            author = chosen[1].get("text", "")
            author_url = chosen[1].get("href", "")
    if not url and chosen:
        url = chosen[0].get("href", "")
    if not title:
        title = detail

    title = _clean(title)
    if any(marker in title for marker in TITLE_NOISE_MARKERS):
        title = ""

    return {
        "title": title,
        "url": _abs_url(base_url, url),
        "thumbnail": _abs_url(base_url, raw.get("img") or ""),
        "detail": _clean(detail),
        "hash": _clean(raw.get("hash") or ""),
        "author": _clean(author),
        "author_url": _abs_url(base_url, author_url),
        "source_mark": mark,
    }


def parse_ascii2d_html(html: str, *, base_url: str = DEFAULT_BASE_URL) -> list[dict]:
    """把 ascii2d 结果页 HTML 解析为规范条目列表（**纯函数，绝不抛异常**）。

    任何残缺 / 异常 HTML 都优雅降级（返回已成功解析的条目，缺失字段留空）。
    """
    if html is None:
        return []
    text = str(html)
    if not text.strip():
        return []

    parser = _Ascii2dHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - HTMLParser 极少抛；仍兜底为「已解析部分」
        logger.debug("[搜图] ascii2d HTML 解析异常（已降级）: %s", exc)
    try:
        parser._finish_item()  # 收尾最后一个未关闭的条目
    except Exception:  # noqa: BLE001
        pass

    finalized: list[dict] = []
    for raw in parser.items:
        try:
            finalized.append(_finalize_item(raw, base_url))
        except Exception:  # noqa: BLE001 - 单条异常不影响其它条目
            continue
    return finalized


def build_result_from_item(item: dict) -> SearchResult:
    """把 ``parse_ascii2d_html`` 的规范条目转换为统一 ``SearchResult``（字段映射见 recon §4）。"""
    item = item or {}
    title = _clean(item.get("title")) or _clean(item.get("detail")) or "(无标题)"
    mark = _clean(item.get("source_mark"))
    source = SOURCE_DISPLAY_NAMES.get(mark, UNKNOWN_SOURCE)
    url = _clean(item.get("url")) or "(无链接)"
    thumbnail = _clean(item.get("thumbnail")) or None
    extra = {
        "detail": _clean(item.get("detail")) or None,
        "hash": _clean(item.get("hash")) or None,
        "author": _clean(item.get("author")) or None,
        "author_url": _clean(item.get("author_url")) or None,
        "source_mark": mark or None,
        "tier": "main",
    }
    return SearchResult(
        title=title,
        source=source,
        url=url,
        thumbnail=thumbnail,
        score=None,  # ascii2d 不返回相似度
        extra=extra,
    )


def parse_ascii2d_response(
    html: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    max_results: int | None = None,
) -> SourceOutcome:
    """解析 ascii2d 结果页 HTML 为统一的 ``SourceOutcome``（纯函数）。

    - ``score`` 一律为 ``None``，**不过滤**、不排序（保持结果页原始顺序）；
    - 空响应 / 无 ``item-box`` → 返回空结果（**不抛异常**），由客户端决定是否报错。
    """
    warnings: list[str] = []
    meta: dict = {"raw_count": 0}

    items = parse_ascii2d_html(html, base_url=base_url)
    meta["raw_count"] = len(items)

    results = [build_result_from_item(item) for item in items]

    if max_results is not None:
        try:
            results = results[: max(0, int(max_results))]
        except (TypeError, ValueError):
            pass

    if not str(html or "").strip():
        warnings.append("ascii2d 站点返回空响应（可能被拦截或接口已变更）。")

    return SourceOutcome(results=results, warnings=warnings, meta=meta)


class Ascii2dClient:
    """ascii2d 以图反查客户端（multipart 上传 + HTML 解析）。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        bovw: bool = False,
        timeout: int = 30,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.bovw = bool(bovw)
        self.timeout = max(5, int(timeout))
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """释放底层 aiohttp 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(total=float(self.timeout), connect=min(15, self.timeout))
                    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5, ttl_dns_cache=300)
                    self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    def build_headers(self) -> dict:
        """构造请求头：浏览器 UA + Referer/Origin + 日文优先的 Accept-Language。"""
        from .image_source import BROWSER_UA  # 局部导入，避免模块级循环依赖

        return {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.8",
            "Referer": f"{self.base_url}/",
            "Origin": self.base_url,
        }

    @staticmethod
    def build_form(image: bytes, *, filename: str = "query.jpg", mime: str = "image/jpeg") -> aiohttp.FormData:
        """构造 multipart 表单（字段名 ``file``）。抽成独立方法便于离线自检字段名。"""
        form = aiohttp.FormData()
        form.add_field("file", image, filename=filename or "query.jpg", content_type=mime or "image/jpeg")
        return form

    async def search(
        self,
        image: bytes,
        *,
        filename: str = "query.jpg",
        mime: str = "image/jpeg",
    ) -> SourceOutcome:
        """上传图片执行反查。

        Args:
            image: 图片二进制。
            filename: 上传文件名（须带扩展名）。
            mime: 图片 MIME 类型。

        Returns:
            解析后的 ``SourceOutcome``。

        Raises:
            ValueError: 图片数据为空。
            RuntimeError: 网络层（超时/传输错误/非 200）、响应为空、bovw 二次请求失败。
        """
        if not image:
            raise ValueError("图片数据为空")

        url = f"{self.base_url}/search/file"
        form = self.build_form(image, filename=filename, mime=mime)
        headers = self.build_headers()
        session = await self._get_session()

        logger.info(
            "[搜图] ascii2d 请求: base=%s path=/search/file bovw=%s bytes=%d",
            self.base_url,
            self.bovw,
            len(image),
        )

        # 网络层：区分超时 / 传输错误 / 非 2xx，统一包装为可读 RuntimeError
        final_url = ""
        try:
            # aiohttp 默认跟随重定向，resp.url 即**最终结果页 URL**（bovw 切换依赖它）
            async with session.post(url, data=form, headers=headers) as resp:
                status = resp.status
                if status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"ascii2d 接口失败 HTTP {status}: {body[:200]}")
                body = await resp.text()
                final_url = str(getattr(resp, "url", "") or "")
        except asyncio.TimeoutError as exc:
            raise RuntimeError("ascii2d 请求超时，请稍后重试（大陆访问通常需配置代理）") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"ascii2d 网络错误（大陆访问通常需配置代理）: {exc}") from exc

        if not body or not body.strip():
            raise RuntimeError("ascii2d 接口返回空响应（可能被拦截或接口已变更）")

        # bovw：把结果页 URL 的 /color/ 换成 /bovw/ 再 GET 一次；URL 不含 /color/ 时优雅降级
        if self.bovw:
            bovw_url = final_url.replace("/color/", "/bovw/")
            if bovw_url and bovw_url != final_url:
                try:
                    async with session.get(bovw_url, headers=headers) as resp2:
                        if resp2.status != 200:
                            body2 = await resp2.text()
                            raise RuntimeError(f"ascii2d bovw 接口失败 HTTP {resp2.status}: {body2[:200]}")
                        body = await resp2.text()
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("ascii2d bovw 请求超时，请稍后重试") from exc
                except aiohttp.ClientError as exc:
                    raise RuntimeError(f"ascii2d bovw 网络错误: {exc}") from exc
                if not body or not body.strip():
                    raise RuntimeError("ascii2d bovw 接口返回空响应（可能被拦截或接口已变更）")
            else:
                logger.info(
                    "[搜图] ascii2d 结果页 URL 不含 /color/，bovw 切换降级为直接解析首次响应: %r",
                    final_url,
                )

        return parse_ascii2d_response(body, base_url=self.base_url)
