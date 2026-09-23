"""统一结果模型与消息格式化。

- ``SearchResult``：不同图源归一化后的单条命中结果。
- ``SourceOutcome``：一次搜索的完整产物（结果列表 + 警告 + 元数据）。
- ``format_outcome``：把一次搜索产物格式化为「消息块」列表（纯数据，便于单测）。
- ``blocks_to_components``：把消息块转换为 AstrBot 消息组件（延迟导入，保持本模块纯净）。

安全约束（硬性）：当 ``nsfw_send_image=False`` 时，``format_outcome`` 绝不产生
``image`` 类型消息块，且不会在文本中泄露缩略图 URL，只输出文字与来源详情链接。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchResult:
    """归一化后的单条搜索结果。"""

    title: str
    source: str
    url: str
    thumbnail: str | None = None
    score: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SourceOutcome:
    """一次搜索的完整产物。"""

    results: list[SearchResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _result_line(index: int, result: SearchResult) -> str:
    """把单条结果渲染为多行文本（不含缩略图 URL）。"""
    title = result.title.strip() if result.title else "（无标题）"
    line = f"{index}. 【{result.source}】{title}"

    if result.score is not None:
        line += f" | 相似度 {result.score:.1f}%"

    extra = result.extra or {}
    page_no = extra.get("page_no")
    if page_no:
        line += f" | 第{page_no}页"

    rating = extra.get("rating")
    if rating:
        line += f" | 评级 {rating}"

    if extra.get("low_confidence"):
        line += " ⚠️低置信度"

    if result.url:
        line += f"\n   🔗 {result.url}"
    return line


def _text_blocks_length(blocks: list[dict]) -> int:
    """统计所有 text 块的总字符数（块之间按单个换行符计）。"""
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    if not texts:
        return 0
    return sum(len(t) for t in texts) + max(0, len(texts) - 1)


def _take_text_blocks(text_blocks: list[dict], budget: int) -> tuple[list[dict], int]:
    """按字符预算依次取 text 块（块间按单换行计），返回 (块列表, 已用字符数)。"""
    out: list[dict] = []
    used = 0
    if budget <= 0:
        return out, used
    for block in text_blocks:
        text = block.get("text", "")
        sep = 1 if out else 0  # 块之间的连接换行
        remaining = budget - used - sep
        if remaining <= 0:
            break
        if len(text) <= remaining:
            out.append(block)
            used += len(text) + sep
        else:
            out.append({"type": "text", "text": text[:remaining]})
            used += remaining + sep
            break
    return out, used


def _truncate_blocks(
    blocks: list[dict],
    max_chars: int,
    suffix: str = "…（结果过长已截断）",
) -> list[dict]:
    """对 text 块做字符边界截断；image 块原样保留。

    硬性约束：任何 ``max_chars >= 0`` 取值下，输出文本总长度都 ``<= max_chars``；
    ``max_chars <= 0`` 表示不限制（原样返回）。当预算装不下后缀时，**省略后缀**只做纯截断，
    以保证上限优先。
    """
    if max_chars <= 0 or _text_blocks_length(blocks) <= max_chars:
        return blocks

    text_blocks = [b for b in blocks if b.get("type") == "text"]
    image_blocks = [b for b in blocks if b.get("type") == "image"]

    # 优先：预算够时带上「换行 + 后缀」
    if max_chars > len(suffix) + 1:
        budget = max_chars - len(suffix) - 1
        out, _used = _take_text_blocks(text_blocks, budget)
        if out:
            out.append({"type": "text", "text": suffix})
            out.extend(image_blocks)
            return out

    # 兜底：预算装不下后缀 → 纯截断，不追加后缀（保证不超过上限）
    out, _used = _take_text_blocks(text_blocks, max_chars)
    if not out and text_blocks:
        out = [{"type": "text", "text": text_blocks[0].get("text", "")[:max_chars]}]
    out.extend(image_blocks)
    return out


def format_outcome(
    outcome: SourceOutcome,
    *,
    nsfw_send_image: bool,
    max_results: int,
    header: str,
    max_chars: int = 1200,
) -> list[dict]:
    """把一次搜索产物格式化为消息块列表。

    Args:
        outcome: 搜索产物。
        nsfw_send_image: 是否附带缩略图（图片组件）。为 ``False`` 时仅输出文字与链接。
        max_results: 最多展示的结果条数。
        header: 首行标题文案。
        max_chars: 文本总长度上限（字符），超出按字符边界截断并追加提示；
            设为 0 或负数表示不限制。

    Returns:
        形如 ``[{"type": "text", "text": ...}, {"type": "image", "url": ...}]`` 的列表。
    """
    blocks: list[dict] = []
    if header:
        blocks.append({"type": "text", "text": header})

    try:
        limit = max(0, int(max_results))
    except (TypeError, ValueError):
        limit = 3
    results = list(outcome.results or [])[:limit]

    if not results:
        blocks.append({"type": "text", "text": "😥 没有找到匹配结果。"})
        for warning in outcome.warnings or []:
            blocks.append({"type": "text", "text": f"⚠️ {warning}"})
    else:
        lines = [_result_line(i, r) for i, r in enumerate(results, start=1)]
        blocks.append({"type": "text", "text": "\n".join(lines)})

        total = len(outcome.results or [])
        if total > limit:
            blocks.append(
                {"type": "text", "text": f"（共 {total} 条命中，仅展示前 {limit} 条）"}
            )

        for warning in outcome.warnings or []:
            blocks.append({"type": "text", "text": f"⚠️ {warning}"})

        # 仅在显式开启时才追加缩略图，且只追加缩略图 URL（绝不改动文本）
        if nsfw_send_image:
            for result in results:
                if result.thumbnail:
                    blocks.append({"type": "image", "url": result.thumbnail})

    return _truncate_blocks(blocks, max_chars)


def blocks_to_components(blocks: list[dict]) -> list:
    """把消息块列表转换为 AstrBot 消息组件列表。

    延迟导入 ``astrbot.api.message_components``，使本模块的解析/格式化逻辑保持可独立测试。
    转换失败的单个块会被跳过，绝不因此中断整条回复。
    """
    from astrbot.api.message_components import Image, Plain  # 延迟导入

    components: list[Any] = []
    for block in blocks or []:
        kind = block.get("type")
        if kind == "text":
            text = block.get("text", "")
            if text:
                components.append(Plain(text))
        elif kind == "image":
            url = block.get("url")
            if not url:
                continue
            try:
                components.append(Image.fromURL(url))
            except Exception:
                # 退化为直接构造，兼容不同 AstrBot 版本
                try:
                    components.append(Image(url))
                except Exception:
                    continue
    return components
