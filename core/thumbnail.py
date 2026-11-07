"""Pixelate sensitive or unrated result thumbnails before sending them."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO

from PIL import Image, ImageOps

from astrbot.api import logger

from .formatter import truncate_blocks
from .image_source import ImageSource


def pixelate_thumbnail(data: bytes, level: int) -> bytes:
    """Create a static PNG with square mosaic blocks over the whole image.

    Args:
        data: Downloaded thumbnail bytes.
        level: Strength from 0 (unchanged bytes) to 10 (large blocks).

    Returns:
        Original bytes at level zero, otherwise a pixelated PNG without metadata.

    Raises:
        ValueError: The level is outside 0-10 or the image exceeds 16 megapixels.
        OSError: Pillow cannot decode the image.
    """
    if not 0 <= level <= 10:
        raise ValueError("Mosaic level must be between 0 and 10")
    if level == 0:
        return data
    with Image.open(BytesIO(data)) as original:
        if original.width * original.height > 16_000_000:
            raise ValueError("Thumbnail exceeds the pixel limit")
        # Only the first frame is emitted; animated originals cannot leak later frames.
        original.seek(0)
        image = ImageOps.exif_transpose(original).convert("RGBA")
    image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    block_size = max(2, round(min(image.size) * (0.01 + 0.29 * (level / 10) ** 2)))
    grid = (max(1, image.width // block_size), max(1, image.height // block_size))
    image = image.resize(grid, Image.Resampling.BOX).resize(
        image.size, Image.Resampling.NEAREST
    )
    output = BytesIO()
    image.info.clear()
    image.save(output, format="PNG")
    return output.getvalue()


async def prepare_thumbnail_blocks(
    blocks: list[dict], image_source: ImageSource, level: int, max_chars: int = 0
) -> list[dict]:
    """Replace marked URLs with processed bytes and omit failed thumbnails.

    Args:
        blocks: Formatted search response blocks, which are never modified in place.
        image_source: Existing downloader with URL, file and image validation.
        level: Configured whole-image mosaic strength from 0 to 10.
        max_chars: Final text budget, including any skipped-thumbnail notice.

    Returns:
        Text and safe images unchanged, with marked images encoded as base64 PNGs.
        Failed images are omitted without falling back to their unprocessed URLs.
    """
    if level == 0:
        return blocks
    prepared: list[dict] = []
    # Deduplicate work within this reply without retaining image bytes across queries.
    processed: dict[str, str | None] = {}
    skipped = False
    for block in blocks:
        if block.get("type") != "image" or not block.get("mosaic"):
            prepared.append(block)
            continue
        url = block.get("url")
        if not isinstance(url, str) or not url.strip():
            skipped = True
            continue
        if url not in processed:
            try:
                payload = await image_source.from_source(url)
                encoded = await asyncio.to_thread(
                    pixelate_thumbnail, payload.data, level
                )
                processed[url] = base64.b64encode(encoded).decode("ascii")
            except Exception as exc:
                logger.warning("Thumbnail pixelation failed: %s", type(exc).__name__)
                processed[url] = None
        if processed[url]:
            prepared.append({"type": "image", "data": processed[url]})
        else:
            skipped = True
    if skipped:
        prepared.append(
            {"type": "text", "text": "部分缩略图无法完成安全处理，已跳过。"}
        )
    return truncate_blocks(prepared, max_chars)
