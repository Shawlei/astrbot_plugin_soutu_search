"""Safebooru 关键词搜图 provider。

接口（实测，无 API Key）::

    GET https://safebooru.org/index.php?page=dapi&s=post&q=index&tags=<关键词>&limit=<N>&pid=<页码>&json=1

- ``pid`` 从 0 开始（分页）
- 多标签用 ``+`` 连接
- 返回 JSON 数组（**无结果时可能是空数组或空响应体**，必须容错）
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import aiohttp

from astrbot.api import logger

from .formatter import SearchResult, SourceOutcome

DEFAULT_BASE_URL = "https://safebooru.org"

# 视为「安全」的 rating（Safebooru 常用 general / safe）
SAFE_RATINGS = {"safe", "general", "s"}


def build_safebooru_url(base_url: str, tags: str, limit: int = 3, page: int = 0) -> str:
    """构造 Safebooru DAPI 请求 URL。"""
    base = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    normalized = " ".join(str(tags).split()).strip()
    encoded_tags = quote(normalized.replace(" ", "+"), safe="+")
    return (
        f"{base}/index.php?page=dapi&s=post&q=index"
        f"&tags={encoded_tags}&limit={int(limit)}&pid={int(page)}&json=1"
    )


def _to_float(value, default: float | None = None) -> float | None:
    """宽松地把值转为 float。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int | None = None) -> int | None:
    """宽松地把值转为 int。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _short_tags(tags: str, max_items: int = 6) -> str:
    """把空格分隔的 tags 截断为可读预览。"""
    if not tags:
        return ""
    parts = [p for p in str(tags).split() if p]
    if not parts:
        return ""
    preview = ", ".join(parts[:max_items])
    if len(parts) > max_items:
        preview += f" …(+{len(parts) - max_items})"
    return preview


def _post_view_url(base_url: str, post_id) -> str:
    """构造 Safebooru 帖子详情页链接。"""
    base = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    if post_id is None:
        return base
    return f"{base}/index.php?page=post&s=view&id={post_id}"


def parse_safebooru_response(
    text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    rating_filter: str = "safe",
    max_results: int | None = None,
) -> SourceOutcome:
    """解析 Safebooru 响应文本为统一的 ``SourceOutcome``（纯函数，便于单测）。"""
    warnings: list[str] = []
    meta: dict = {"filtered_by_rating": 0, "raw_count": 0}

    # 无结果时常见：空字符串 / 空数组 / 非 JSON 内容
    if text is None or not str(text).strip():
        return SourceOutcome(results=[], warnings=["关键词无匹配结果（站点返回空响应）。"], meta=meta)

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return SourceOutcome(
            results=[],
            warnings=["关键词无匹配结果（站点返回非 JSON 内容，可能被限流）。"],
            meta=meta,
        )

    if not isinstance(data, list):
        return SourceOutcome(results=[], warnings=["站点返回数据结构异常。"], meta=meta)

    meta["raw_count"] = len(data)
    results: list[SearchResult] = []

    for post in data:
        if not isinstance(post, dict):
            continue

        rating = str(post.get("rating", "") or "").strip().lower()
        if rating_filter != "all" and rating and rating not in SAFE_RATINGS:
            meta["filtered_by_rating"] += 1
            continue

        post_id = post.get("id")
        tags = post.get("tags") or ""
        thumbnail = post.get("preview_url") or post.get("sample_url") or post.get("file_url") or None

        extra = {
            "rating": rating or None,
            "tags": tags,
            "width": _to_int(post.get("width")),
            "height": _to_int(post.get("height")),
            "sample_url": post.get("sample_url"),
            "file_url": post.get("file_url"),
            "source": post.get("source"),
            "id": post_id,
            "tier": "main",
            "low_confidence": False,
        }

        results.append(
            SearchResult(
                title=_short_tags(tags),
                source="Safebooru",
                url=_post_view_url(base_url, post_id),
                thumbnail=thumbnail,
                score=_to_float(post.get("score"), None),
                extra=extra,
            )
        )

    if max_results is not None:
        try:
            results = results[: max(0, int(max_results))]
        except (TypeError, ValueError):
            pass

    if not results and meta["filtered_by_rating"]:
        warnings.append(f"共 {meta['filtered_by_rating']} 条结果因评级过滤被隐藏（当前为 safety-only）。")

    return SourceOutcome(results=results, warnings=warnings, meta=meta)


class SafebooruClient:
    """Safebooru 关键词搜图客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: int = 30,
        rating: str = "safe",
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = max(5, int(timeout))
        self.rating = "all" if str(rating).strip().lower() == "all" else "safe"
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

    async def search_by_tags(
        self,
        tags: str,
        *,
        limit: int = 3,
        page: int = 0,
        rating: str | None = None,
    ) -> SourceOutcome:
        """按关键词搜索图片。"""
        keyword = " ".join(str(tags or "").split()).strip()
        if not keyword:
            raise ValueError("关键词不能为空")

        final_rating = (rating or self.rating)
        final_rating = "all" if str(final_rating).strip().lower() == "all" else "safe"
        url = build_safebooru_url(self.base_url, keyword, limit=limit, page=page)

        # 请求头复用浏览器 UA，避免被简单反爬拦截
        from .image_source import BROWSER_UA  # 局部导入，避免模块级循环依赖

        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/javascript, */*;q=0.1",
            "Referer": f"{self.base_url}/",
        }

        session = await self._get_session()
        logger.info("[搜图] safebooru 请求: tags=%r limit=%s page=%s rating=%s", keyword, limit, page, final_rating)
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"safebooru 接口失败 HTTP {resp.status}: {text[:200]}")
                body = await resp.text()
        except asyncio.TimeoutError as exc:
            raise RuntimeError("safebooru 请求超时，请稍后重试") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"safebooru 网络错误: {exc}") from exc

        return parse_safebooru_response(
            body,
            base_url=self.base_url,
            rating_filter=final_rating,
        )
