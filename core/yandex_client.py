"""Yandex（yandex.ru）以图搜图反查 provider。

用途：全网多源以图搜图，对画师作品、Twitter、Pixiv、Booru 搬运站以及跨平台转载
收录极快、召回率极高。不需要任何 API Key。

接口说明：
- 默认端点使用 ``https://yandex.ru``（相较 yandex.com，对反爬与区域限制更友好）。
- 发送方式：POST 到 ``/images/search?rpt=imageview&format=json&request={...}``
  multipart 表单必须包含明确的 ``Content-Length``（预先拼接 bytes 避免 chunked 触发 413）。
- 响应：直接从服务端渲染（SSR）的 ``data-state`` 中提取 ``cbirSites.sites``（引用该图片的网页）
  以及 ``cbirSimilar.thumbs``（相似图）。

**来源质量排序（本插件对 Yandex 的关键增强）**
Yandex 对二次元插画精确溯源固有偏弱：``cbirSites.sites`` 会把「视觉相似」网页排在前面，
把真正包含该图的图库 / 官方 / 画师平台混在中后部（实测 107 条里 Pinterest 系占 57 条，
真·图库来源在第 40~105 位）。若像早期实现那样盲取 ``sites[:top_k]``，用户只会看到一堆
Pinterest 相似图，一条有价值的来源都看不到。因此本模块对 ``sites`` 做：

1. **来源质量分级**：按域名后缀把来源分为「高价值 / 中性 / 低价值」，排序时高价值优先；
2. **域名去重**：同一域名（并把 Pinterest 全系聚合成一个家族）最多保留 ``max_per_domain`` 条，
   去重**在排序之后**进行（保留每个域名质量最高的那几条）；
3. **兜底不空**：高价值来源不足时，按「中性 → 低价值」顺序补足到 ``top_k``，
   绝不因为过滤把结果变空（用户至少应看到相似图线索）。
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

# --------------------------------------------------------------------------- #
# 来源质量分级
# --------------------------------------------------------------------------- #
# 质量等级（对外可读的字符串常量）
LEVEL_HIGH = "high"        # 高价值：图库 / 官方 / 画师平台 / 资料站
LEVEL_NEUTRAL = "neutral"  # 中性：无法归类的普通站点
LEVEL_LOW = "low"          # 低价值：Pinterest 全系、聚合搬运站、内容农场

# 排序权重：数值越小越靠前（高价值 → 中性 → 低价值）
_LEVEL_RANK: dict[str, int] = {
    LEVEL_HIGH: 0,
    LEVEL_NEUTRAL: 1,
    LEVEL_LOW: 2,
}

# 每个域名在一次结果里最多保留的条数（可被 ``parse_yandex_html(max_per_domain=...)`` 覆盖）。
DEFAULT_MAX_PER_DOMAIN = 2

# 高价值来源：图库 / 官方 / 画师平台 / 资料站。
# 采用「域名后缀匹配」：``host == suffix`` 或 ``host.endswith('.' + suffix)``。
# 因此 ``danbooru.donmai.us`` 命中后，其任意子域（如 ``hijiribe.donmai.us``）也会因
# 命中 ``donmai.us`` 而被判为高价值。
_HIGH_VALUE_SUFFIXES: frozenset[str] = frozenset(
    {
        # —— booru 图库系 ——
        "danbooru.donmai.us",
        "donmai.us",          # 覆盖 hijiribe.donmai.us 等 donmai.us 子站
        "donmai.moe",
        "safebooru.org",
        "safebooru.donmai.us",
        "yande.re",
        "konachan.net",
        "konachan.com",
        "gelbooru.com",
        # —— 画师首发 / 官方 / 创作平台 ——
        "pixiv.net",
        "twitter.com",
        "x.com",
        "skeb.jp",
        "artstation.com",
        "fanbox.cc",
        "fantia.jp",
        "misskey.io",
        "nico.ms",
        # —— 资料 / 百科站 ——
        "wikipedia.org",
        "fandom.com",
        "anidb.net",
        "myanimelist.net",
    }
)

# 低价值来源（后缀匹配）：聚合搬运站、内容农场、明显 scraper 站。
_LOW_VALUE_SUFFIXES: frozenset[str] = frozenset(
    {
        "tumblr.com",
        "reactor.cc",
        "joyreactor.cc",
        "wattpad.com",
        "seputarundip.com",
        "i-model.org",
    }
)

# 低价值来源（品牌标签匹配）：Pinterest 有数十个国别/TLD 变体
# （pinterest.com / .ru / .ca / .co / .ar / .uk / .za / .in / .fi / .tr / .id …），
# 逐一枚举后缀既繁琐又易漏，故改为「品牌标签匹配」：只要 host 的某个 DNS 标签
# 等于 ``pinterest`` 即判为低价值。这样 ``za.pinterest.com``、``pinterest.co.uk``
# 都能命中，而 ``notpinterest.com`` 不会（标签不同）。
_LOW_VALUE_BRANDS: frozenset[str] = frozenset({"pinterest"})

# 「域名去重」时按品牌聚合的家族：把 Pinterest 全系（跨 TLD）视为同一桶，
# 避免 19 条 pinterest.com + 12 条 ru.pinterest.com 之类占满输出。
# 注意：高价值图库**不**聚合（donmai.moe / donmai.us / safebooru.org 是不同站点，
# 各自可能含不同信息，应分别保留）。
_FAMILY_BRANDS: frozenset[str] = frozenset({"pinterest"})

# 当一次结果里没有任何高价值来源时的诚实提示（避免把相似图伪装成「找到了来源」）。
# 该文案会被 ``SourceOutcome.warnings`` 承接，最终由 ``format_outcome`` 统一加 ``⚠️`` 前缀渲染，
# 因此此处**刻意不自带** ``⚠️``（避免重复）。
SIMILAR_ONLY_WARNING = (
    "未找到包含该图的图库来源，以下为视觉相似图（可尝试 SauceNAO 精确反查）"
)


def _normalize_domain(domain: str) -> str:
    """把域名 / 主机名归一化为小写、去协议、去路径、去端口、去首尾点的纯主机名。

    容错：非字符串 / ``None`` / 空白 → 返回空串；任何异常都吞掉并返回已处理的部分，
    绝不向外抛出。
    """
    try:
        text = str(domain or "").strip().lower()
    except Exception:  # noqa: BLE001 - ``str`` 对畸形对象也可能抛
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    if "@" in text:  # 去 userinfo
        text = text.split("@", 1)[1]
    text = text.split("/", 1)[0]  # 去路径 / 查询
    text = text.split("?", 1)[0]
    text = text.split(":", 1)[0]  # 去端口
    return text.strip(".")


def _matches_suffix(host: str, suffix: str) -> bool:
    """域名后缀匹配：``host`` 等于 ``suffix``，或为其子域（``*.suffix``）。

    刻意**不做**「子串包含」判断，避免 ``notdanbooru.donmai.us.evil.com`` 之类的伪装命中。
    """
    return host == suffix or host.endswith("." + suffix)


def _matches_brand(host: str, brand: str) -> bool:
    """品牌标签匹配：``host`` 的某个 DNS 标签恰好等于 ``brand``。

    用于跨 TLD 的品牌家族（如 Pinterest 的 ``pinterest.*``）。标签级比较可避免
    ``notpinterest.com`` 被误判。
    """
    return brand in host.split(".")


def classify_source(domain: str) -> str:
    """把来源域名分级为 :data:`LEVEL_HIGH` / :data:`LEVEL_NEUTRAL` / :data:`LEVEL_LOW`。

    判定顺序：高价值后缀 → 低价值后缀 → 低价值品牌 → 中性（无法归类）。
    无法归类的域名归为**中性**，排在「高价值」与「低价值」之间：它们不像 Pinterest 那样
    是纯粹的搬运噪声，但也无法确认是权威图库，故给出一个折中位置而非直接丢弃。
    """
    host = _normalize_domain(domain)
    if not host:
        return LEVEL_NEUTRAL
    for suffix in _HIGH_VALUE_SUFFIXES:
        if _matches_suffix(host, suffix):
            return LEVEL_HIGH
    for suffix in _LOW_VALUE_SUFFIXES:
        if _matches_suffix(host, suffix):
            return LEVEL_LOW
    for brand in _LOW_VALUE_BRANDS:
        if _matches_brand(host, brand):
            return LEVEL_LOW
    return LEVEL_NEUTRAL


def _dedup_key(domain: str) -> str:
    """计算「域名去重」用的桶键。

    - 属于已知品牌家族（Pinterest）→ 聚合为单一桶（如所有 ``*.pinterest.*`` → ``pinterest``）；
    - 其余 → 归一化主机名（不同站点各自成桶）。
    """
    host = _normalize_domain(domain)
    if not host:
        return ""
    for brand in _FAMILY_BRANDS:
        if _matches_brand(host, brand):
            return brand
    return host


def compute_level_rank(domain: str) -> int:
    """来源排序权重（越小越靠前），便于外部复用。"""
    return _LEVEL_RANK[classify_source(domain)]


def build_yandex_warnings(results: list[SearchResult] | None) -> list[str]:
    """根据结果集构造 ``SourceOutcome.warnings``。

    - 结果为空 → 空列表（「没有找到匹配结果」由 formatter 负责）；
    - 结果里**存在**任一高价值来源 → 空列表（找到了图库/官方来源，无需提示）；
    - 结果非空但**没有任何高价值来源**（全是 Pinterest 之类相似图噪声，或
      ``cbirSimilar`` 兜底的视觉相似图）→ 加一条 :data:`SIMILAR_ONLY_WARNING`，
      诚实告知用户「这不是找到了来源」。
    """
    if not results:
        return []
    for result in results:
        if classify_source(getattr(result, "source", "")) == LEVEL_HIGH:
            return []
    return [SIMILAR_ONLY_WARNING]


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


def _extract_initial_state(html_text: str) -> dict | None:
    """从结果页 HTML 中提取内嵌的 ``initialState``（HTML 转义后的 JSON）。

    与既有实现保持一致：正则捕获所有长度 ≥300 的 ``data-state``，逐个 ``unescape`` + ``json.loads``，
    取第一个含 ``initialState`` 的 dict；解析失败全部跳过，绝不抛异常。
    """
    if not html_text:
        return None
    states = re.findall(r'data-state="([^"]{300,})"', html_text)
    for raw in states:
        try:
            obj = json.loads(html.unescape(raw))
        except Exception:
            continue
        if isinstance(obj, dict) and "initialState" in obj:
            init_state = obj["initialState"]
            if isinstance(init_state, dict):
                return init_state
    return None


def _coerce_max_per_domain(value) -> int:
    """把 ``max_per_domain`` 归一化为正整数；非法或 ≤0 → :data:`DEFAULT_MAX_PER_DOMAIN`。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PER_DOMAIN
    return parsed if parsed > 0 else DEFAULT_MAX_PER_DOMAIN


def _coerce_top_k(value) -> int:
    """把 ``top_k`` 归一化为 ≥1 的整数；非法或 ≤0 → 1（保证至少给一条）。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def _parse_sites(init_state: dict, top_k: int, max_per_domain: int) -> list[SearchResult]:
    """解析 ``cbirSites.sites``：URL 去重 → 质量分级排序 → 域名去重 → 截断到 ``top_k``。"""
    cbir_sites = init_state.get("cbirSites")
    sites = cbir_sites.get("sites", []) if isinstance(cbir_sites, dict) else []
    if not isinstance(sites, list):
        return []

    # (排序权重, 原始序号, 结果, 去重桶键)
    candidates: list[tuple[int, int, SearchResult, str]] = []
    seen_urls: set[str] = set()

    for index, site in enumerate(sites):
        if not isinstance(site, dict):
            continue
        raw_url = site.get("url") or ""
        if not isinstance(raw_url, str):
            continue
        url = clean_url(raw_url)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        title_value = site.get("title") or site.get("description") or "未知标题"
        title = str(title_value).strip() or "未知标题"
        domain = str(site.get("domain") or "网页来源").strip() or "网页来源"
        thumb_obj = site.get("thumb")
        thumb_url = thumb_obj.get("url") if isinstance(thumb_obj, dict) else ""
        if not isinstance(thumb_url, str):
            thumb_url = ""
        if thumb_url.startswith("//"):
            thumb_url = "https:" + thumb_url

        level = classify_source(domain)
        result = SearchResult(
            title=title,
            source=domain,
            url=url,
            thumbnail=thumb_url or None,
            score=None,  # Yandex 无相似度，恒为 None
            extra={"source_level": level},
        )
        candidates.append((_LEVEL_RANK[level], index, result, _dedup_key(domain)))

    if not candidates:
        return []

    # 稳定排序：先按质量等级（高→中→低），同等级按 Yandex 原始出现顺序。
    candidates.sort(key=lambda item: (item[0], item[1]))

    # 域名去重（在排序之后）：保留每个域名质量最高的若干条。
    results: list[SearchResult] = []
    per_domain: dict[str, int] = {}
    for _rank, _index, result, key in candidates:
        if per_domain.get(key, 0) >= max_per_domain:
            continue
        per_domain[key] = per_domain.get(key, 0) + 1
        results.append(result)
        if len(results) >= top_k:
            break

    return results


def _parse_similar(init_state: dict, base_url: str, top_k: int) -> list[SearchResult]:
    """兜底路径：``sites`` 为空时解析 ``cbirSimilar.thumbs``（视觉相似图）。

    每项取 ``linkUrl``（相对路径补 ``base_url``）或 ``imageUrl``；``source`` 标记为
    ``yandex-similar``。逐项容错，非 dict / 空 URL 跳过。
    """
    thumbs = init_state.get("cbirSimilar")
    thumbs = thumbs.get("thumbs", []) if isinstance(thumbs, dict) else []
    if not isinstance(thumbs, list):
        return []

    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for thumb in thumbs:
        if not isinstance(thumb, dict):
            continue
        raw_url = thumb.get("linkUrl") or thumb.get("imageUrl") or ""
        if not isinstance(raw_url, str):
            continue
        if raw_url.startswith("/"):
            raw_url = base_url.rstrip("/") + raw_url
        url = clean_url(raw_url)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        title_value = thumb.get("title") or "相似图片"
        title = str(title_value).strip() or "相似图片"
        results.append(
            SearchResult(
                title=title,
                source="yandex-similar",
                url=url,
                thumbnail=None,
                score=None,
                extra={"source_level": LEVEL_LOW},
            )
        )
        if len(results) >= top_k:
            break

    return results


def parse_yandex_html(
    html_text: str,
    base_url: str = DEFAULT_BASE_URL,
    top_k: int = 5,
    max_per_domain: int = DEFAULT_MAX_PER_DOMAIN,
) -> list[SearchResult]:
    """从 Yandex 搜索结果页 HTML 中提取匹配结果。

    处理顺序：
    1. 优先解析精确引用该图的网站（``cbirSites.sites``）——经质量分级排序 + 域名去重；
    2. 若 ``sites`` 为空（或未产出任何有效结果），兜底取相似图（``cbirSimilar.thumbs``）。

    容错：空 HTML / 无 ``initialState`` / 残结构 / 非 dict 项全部优雅降级，绝不抛异常。

    Args:
        html_text: 结果页 HTML。
        base_url: 兜底相似图相对路径补全用的 base。
        top_k: 最多返回条数（含兜底）。
        max_per_domain: 单个域名（Pinterest 系合并为一个域）最多保留条数；≤0 或非法回退默认值。
    """
    if not html_text or not isinstance(html_text, str):
        return []

    init_state = _extract_initial_state(html_text)
    if init_state is None:
        return []

    top_k = _coerce_top_k(top_k)
    max_per_domain = _coerce_max_per_domain(max_per_domain)

    results = _parse_sites(init_state, top_k, max_per_domain)
    if results:
        return results

    return _parse_similar(init_state, base_url, top_k)


class YandexClient:
    """Yandex 识图反查客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
        top_k: int = 5,
        max_per_domain: int = DEFAULT_MAX_PER_DOMAIN,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = max(5, int(timeout))
        self.top_k = max(1, min(30, int(top_k)))
        self.max_per_domain = _coerce_max_per_domain(max_per_domain)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            client_timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=client_timeout)
        return self._session

    def _parse(self, html_text: str) -> list[SearchResult]:
        """统一的解析入口（带上本客户端的 ``top_k`` / ``max_per_domain``）。"""
        return parse_yandex_html(
            html_text,
            base_url=self.base_url,
            top_k=self.top_k,
            max_per_domain=self.max_per_domain,
        )

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
                    results = self._parse(resp_text)
                else:
                    parsed = json.loads(resp_text)
                    cbir_id = parsed.get("blocks", [{}])[0].get("params", {}).get("cbirId")
                    if not cbir_id:
                        raise RuntimeError("未能从 Yandex 响应中获取 cbirId")
                    get_url = (
                        f"{self.base_url}/images/search"
                        f"?rpt=imageview&cbir_id={urllib.parse.quote(cbir_id)}&cbir_page=sites"
                    )
                    async with session.get(get_url, headers=headers) as get_resp:
                        if get_resp.status != 200:
                            raise RuntimeError(f"Yandex 获取结果页失败 HTTP {get_resp.status}")
                        html_text = await get_resp.text()
                        results = self._parse(html_text)

        except asyncio.TimeoutError as exc:
            logger.warning("[搜图] Yandex 请求超时 (%ss)", self.timeout)
            raise TimeoutError(f"Yandex 请求超时（{self.timeout}s），请稍后重试") from exc
        except aiohttp.ClientError as exc:
            logger.warning("[搜图] Yandex 网络请求失败: %s", exc)
            raise RuntimeError(f"Yandex 网络请求失败: {exc}") from exc

        return SourceOutcome(results=results, warnings=build_yandex_warnings(results))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
