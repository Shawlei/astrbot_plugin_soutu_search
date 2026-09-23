"""搜图Bot酱（soutubot.moe）以图搜图 provider。

接口（实测，无签名、无 API Key）::

    POST https://soutubot.moe/api/search
    Content-Type: multipart/form-data
    fields: file / factor / metadata_mode / top_k
    headers: Accept, Accept-Language, Referer, Origin, User-Agent

响应遵循 ``schema_version 2.2``。本模块对响应做完整容错解析：
- 命中判定：``results[].path_segments`` 非空才算命中
- 相似度阈值：``factor==1.4`` 时为 35，否则为 45；低于阈值标注「低置信度」
- 结果分档：``score >= 28`` 进主列表，``< 28`` 归为低分结果
- 标题回退链：``metadata.title.primary`` → ``metadata.title``(平铺字符串) → ``metadata.title.japanese_or_alias``
- 链接优先级：``page_url`` → ``chapter_url`` → ``source_url`` → ``metadata.source.url``
- 所有字段均以 ``.get()`` 取值并给默认值，绝不因字段缺失抛 ``KeyError``
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from astrbot.api import logger

from .formatter import SearchResult, SourceOutcome

DEFAULT_BASE_URL = "https://soutubot.moe"

# 置信度阈值（来自前端 renderResults 逻辑）
CONFIDENCE_THRESHOLD_STRICT = 35.0  # factor == "1.4"
CONFIDENCE_THRESHOLD_NORMAL = 45.0  # 其它 factor
# 主列表分档阈值
MAIN_TIER_THRESHOLD = 28.0

# source_key -> 中文可读名
SOURCE_NAME_MAP: dict[str, str] = {
    "nhentai": "NH本子",
    "ehentai": "E站",
    "jmcomic": "禁漫",
    "manhuacat": "漫画猫",
    "gelbooru": "Gelbooru",
    "yande": "Yande.re",
    "panda": "熊猫图库",
    "zerochan": "Zerochan",
    "pixiv": "Pixiv",
    "danbooru": "Danbooru",
    "konachan": "Konachan",
    "safebooru": "Safebooru",
}


def source_display_name(source_key: str | None) -> str:
    """把 source_key 映射为中文可读名；未知的返回原文或「未知来源」。"""
    if not source_key:
        return "未知来源"
    key = str(source_key).strip()
    return SOURCE_NAME_MAP.get(key.lower(), key or "未知来源")


def resolve_threshold(factor: Any) -> float:
    """根据 factor 返回置信度阈值。"""
    text = str(factor).strip()
    if text.startswith("1.4"):
        return CONFIDENCE_THRESHOLD_STRICT
    return CONFIDENCE_THRESHOLD_NORMAL


def _to_float(value: Any, default: float | None = None) -> float | None:
    """宽松地把值转为 float；失败返回默认值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_title(metadata: dict) -> str:
    """从 metadata 中按回退链提取标题。"""
    if not isinstance(metadata, dict):
        return ""
    title = metadata.get("title")

    # 形态一：平铺字符串
    if isinstance(title, str):
        return title.strip()

    # 形态二：结构化 dict（primary -> japanese_or_alias）
    if isinstance(title, dict):
        primary = title.get("primary")
        if isinstance(primary, str) and primary.strip():
            return primary.strip()
        alias = title.get("japanese_or_alias")
        if isinstance(alias, str) and alias.strip():
            return alias.strip()
    return ""


def extract_url(segment: dict, metadata: dict) -> str:
    """按优先级提取详情页链接。"""
    if isinstance(segment, dict):
        for key in ("page_url", "chapter_url", "source_url"):
            value = segment.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    if isinstance(metadata, dict):
        source = metadata.get("source")
        if isinstance(source, dict):
            value = source.get("url")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _extract_tags(metadata: dict) -> list[str]:
    """提取 tags（可能是字符串或字符串列表）。"""
    if not isinstance(metadata, dict):
        return []
    tags = metadata.get("tags")
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    if isinstance(tags, str) and tags.strip():
        return [t for t in (piece.strip() for piece in tags.split(",")) if t]
    return []


def parse_soutu_response(
    payload: Any,
    factor: Any = "1.2",
    min_score: float = MAIN_TIER_THRESHOLD,
) -> SourceOutcome:
    """解析 soutubot 响应为统一的 ``SourceOutcome``（纯函数，便于单测）。"""
    warnings: list[str] = []
    meta: dict = {}

    if not isinstance(payload, dict):
        return SourceOutcome(results=[], warnings=["接口返回数据格式异常"], meta=meta)

    meta["schema_version"] = payload.get("schema_version")
    meta["result_id"] = payload.get("result_id")

    raw_warnings = payload.get("warnings")
    if isinstance(raw_warnings, list):
        warnings.extend(str(w) for w in raw_warnings if str(w).strip())

    partial = bool(payload.get("partial")) or str(payload.get("status", "")).lower() == "partial"
    meta["partial"] = partial
    if partial:
        warnings.append("站点返回部分结果（partial），命中可能不完整。")

    threshold = resolve_threshold(factor)
    meta["threshold"] = threshold

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = []

    collected: list[SearchResult] = []
    hit_count = 0

    for item in raw_results:
        if not isinstance(item, dict):
            continue
        segments = item.get("path_segments")
        if not isinstance(segments, list) or not segments:
            continue  # 无 path_segments 视为未命中
        hit_count += 1
        score = _to_float(item.get("score"), None)

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            metadata = segment.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}

            source_obj = metadata.get("source")
            source_key = segment.get("source_key")
            if not source_key and isinstance(source_obj, dict):
                source_key = source_obj.get("key")
            source_key = str(source_key).strip() if source_key else ""

            low_confidence = score is None or score < threshold
            tier = "main" if (score is not None and score >= MAIN_TIER_THRESHOLD) else "low"

            extra = {
                "source_key": source_key,
                "page_no": segment.get("page_no"),
                "chapter_no": segment.get("chapter_no"),
                "language": segment.get("language"),
                "display_kind": metadata.get("display_kind"),
                "metadata_status": segment.get("metadata_status"),
                "japanese_title": (
                    metadata.get("title", {}).get("japanese_or_alias")
                    if isinstance(metadata.get("title"), dict)
                    else None
                ),
                "tags": _extract_tags(metadata),
                "low_confidence": low_confidence,
                "tier": tier,
            }

            collected.append(
                SearchResult(
                    title=extract_title(metadata),
                    source=source_display_name(source_key),
                    url=extract_url(segment, metadata),
                    thumbnail=segment.get("thumbnail_url") or None,
                    score=score,
                    extra=extra,
                )
            )

    # 按分数降序（None 排最后）
    collected.sort(key=lambda r: (r.score is not None, r.score if r.score is not None else 0.0), reverse=True)

    # 低于 min_score 的过滤掉（分数未知的保留，交由上层判定）
    filtered = [r for r in collected if r.score is None or r.score >= float(min_score)]

    meta["hit_count"] = hit_count
    meta["total_segments"] = len(collected)
    meta["shown_segments"] = len(filtered)

    if filtered and all(r.extra.get("low_confidence") for r in filtered):
        warnings.append(f"最佳相似度低于置信阈值（{threshold:.0f}），结果仅供参考。")

    return SourceOutcome(results=filtered, warnings=warnings, meta=meta)


class SoutuClient:
    """搜图Bot酱以图搜图客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        factor: str = "1.2",
        timeout: int = 30,
        min_score: float = MAIN_TIER_THRESHOLD,
        top_k: int = 25,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.factor = "1.4" if str(factor).strip().startswith("1.4") else "1.2"
        self.timeout = max(5, int(timeout))
        self.min_score = float(min_score)
        self.top_k = max(1, int(top_k))
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

    async def search(
        self,
        image: bytes,
        *,
        filename: str = "query.jpg",
        mime: str = "image/jpeg",
        factor: str | None = None,
        top_k: int | None = None,
    ) -> SourceOutcome:
        """上传图片执行以图搜图。

        Args:
            image: 图片二进制。
            filename: 上传文件名（必须带扩展名）。
            mime: 图片 MIME 类型。
            factor: 覆盖默认搜索模式（"1.2" / "1.4"）。
            top_k: 覆盖默认返回候选数。

        Returns:
            解析后的 ``SourceOutcome``。
        """
        if not image:
            raise ValueError("图片数据为空")

        final_factor = "1.4" if str(factor or self.factor).strip().startswith("1.4") else "1.2"
        final_top_k = max(1, int(top_k or self.top_k))
        url = f"{self.base_url}/api/search"

        form = aiohttp.FormData()
        form.add_field("file", image, filename=filename or "query.jpg", content_type=mime or "image/jpeg")
        form.add_field("factor", final_factor)
        form.add_field("metadata_mode", "display")
        form.add_field("top_k", str(final_top_k))

        headers = {
            "Accept": "application/json",
            "Accept-Language": "zh-CN",
            "Referer": f"{self.base_url}/",
            "Origin": self.base_url,
        }

        session = await self._get_session()
        logger.info("[搜图] soutubot 请求: factor=%s top_k=%s bytes=%d", final_factor, final_top_k, len(image))

        # 网络层：区分「超时」「传输错误」「HTTP 状态码异常」，统一包装为 RuntimeError
        try:
            async with session.post(url, data=form, headers=headers) as resp:
                status = resp.status
                if status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"soutubot 接口失败 HTTP {status}: {body[:200]}")
                body = await resp.text()
        except asyncio.TimeoutError as exc:
            raise RuntimeError("soutubot 请求超时，请稍后重试") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"soutubot 网络错误: {exc}") from exc

        # 解析层：Cloudflare 可能以 200 返回 HTML 挑战页，此处必须显式报错，
        # 不能静默返回空结果（否则用户会误以为「没搜到」）。
        # 注意：与 Safebooru 侧刻意不对称——Safebooru 无结果时本就返回空/非 JSON，
        # 属正常容错；而 soutubot 正常一定返回 JSON，非 JSON 即异常，需上报。
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "soutubot 接口返回非 JSON（可能被 Cloudflare 拦截或接口已变更）"
            ) from exc

        return parse_soutu_response(payload, factor=final_factor, min_score=self.min_score)
