"""Check label decisions, pixelation and safe thumbnail delivery failures."""

from __future__ import annotations

import base64
import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, sentinel

from PIL import Image as PILImage
from PIL import PngImagePlugin

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for directory in (PLUGIN_ROOT.parent, PLUGIN_ROOT):
    sys.path.insert(0, str(directory))

import tests.test_core as _stub  # noqa: E402,F401
from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    blocks_to_components,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.soutu_client import (  # noqa: E402
    parse_soutu_response,
)
from astrbot_plugin_soutu_search.core.thumbnail import (  # noqa: E402
    pixelate_thumbnail,
    prepare_thumbnail_blocks,
)
from astrbot_plugin_soutu_search.main import SoutuSearchPlugin  # noqa: E402


class ThumbnailTests(unittest.IsolatedAsyncioTestCase):
    """Use synthetic images and mocked downloads without external services."""

    def setUp(self):
        """Create a gradient thumbnail shared by the pixelation tests."""
        image = PILImage.new("RGB", (160, 120))
        image.putdata([(x, y, (x + y) % 256) for y in range(120) for x in range(160)])
        output = BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("comment", "Original metadata")
        image.save(output, format="PNG", pnginfo=metadata)
        self.original = output.getvalue()
        self.downloader = SimpleNamespace(
            from_source=AsyncMock(return_value=SimpleNamespace(data=self.original))
        )

    def test_rating_and_tag_decisions(self):
        """Only explicit safety permits originals; sensitive markers take priority."""
        cases = [
            ({"rating": "safe"}, False),
            ({"rating": "GENERAL"}, False),
            ({"rating": "s"}, False),
            ({"tags": ["SFW"]}, False),
            ({"tags": "全年龄, comedy"}, False),
            ({"tags": ["R-18"]}, True),
            ({"tags": ["r_18g"]}, True),
            ({"tags": ["NSFW", "safe"]}, True),
            ({"rating": "safe", "tags": ["R18"]}, True),
            ({"rating": "safe", "nsfw": True}, True),
            ({"rating": "questionable", "tags": ["safe"]}, True),
            ({"rating": "explicit"}, True),
            ({"tags": ["comedy", "romance"]}, True),
            ({"tags": [{"name": "safe"}]}, True),
            ({}, True),
        ]
        for extra, mosaic in cases:
            with self.subTest(extra=extra):
                outcome = SourceOutcome(
                    results=[
                        SearchResult(
                            "Title",
                            "Source",
                            "",
                            "https://example.com/t.png",
                            extra=extra,
                        )
                    ]
                )
                blocks = format_outcome(
                    outcome,
                    nsfw_send_image=True,
                    thumbnail_mosaic=True,
                    max_results=1,
                    header="Results",
                )
                thumbnail = next(block for block in blocks if block["type"] == "image")
                self.assertEqual(bool(thumbnail.get("mosaic")), mosaic)

    def test_provider_preserves_optional_rating_and_flags(self):
        """Soutu labels reach the formatter without assuming absent labels are safe."""
        for metadata in ({"rating": "safe"}, {"tags": ["R18"], "nsfw": True}, {}):
            with self.subTest(metadata=metadata):
                payload = {
                    "results": [
                        {
                            "score": 80,
                            "path_segments": [
                                {
                                    "metadata": metadata,
                                    "thumbnail_url": "https://example.com/t.png",
                                }
                            ],
                        }
                    ]
                }
                outcome = parse_soutu_response(payload)
                self.assertEqual(
                    outcome.results[0].extra["rating"], metadata.get("rating")
                )
                self.assertEqual(outcome.results[0].extra["nsfw"], metadata.get("nsfw"))

    def test_strength_preserves_size_and_increases_block_size(self):
        """Higher levels remove more detail without stretching the thumbnail."""
        unique_colors = []
        for level in (1, 7, 10):
            with PILImage.open(
                BytesIO(pixelate_thumbnail(self.original, level))
            ) as image:
                self.assertEqual(image.size, (160, 120))
                self.assertEqual(image.format, "PNG")
                self.assertNotIn("comment", image.info)
                unique_colors.append(
                    len(
                        {
                            image.getpixel((x, y))
                            for y in range(image.height)
                            for x in range(image.width)
                        }
                    )
                )
        self.assertGreater(unique_colors[0], unique_colors[1])
        self.assertGreater(unique_colors[1], unique_colors[2])

    def test_zero_strength_and_invalid_levels(self):
        """Zero is explicitly unmodified, while invalid levels are rejected."""
        self.assertEqual(pixelate_thumbnail(self.original, 0), self.original)
        for level in (-1, 11):
            with self.subTest(level=level), self.assertRaises(ValueError):
                pixelate_thumbnail(self.original, level)

    def test_animated_thumbnails_emit_only_processed_first_frame(self):
        """Later animation frames never survive in the sent PNG."""
        output = BytesIO()
        first = PILImage.new("RGB", (40, 40), "red")
        second = PILImage.new("RGB", (40, 40), "blue")
        first.save(
            output, format="GIF", save_all=True, append_images=[second], duration=100
        )
        with PILImage.open(BytesIO(pixelate_thumbnail(output.getvalue(), 7))) as image:
            self.assertEqual(image.n_frames, 1)
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.convert("RGB").getpixel((0, 0)), (255, 0, 0))

    async def test_processed_bytes_replace_original_url_once_per_reply(self):
        """Duplicate thumbnails reuse work and originals are absent from output."""
        original_block = {
            "type": "image",
            "url": "https://example.com/t.png",
            "mosaic": True,
        }
        blocks = [
            {"type": "text", "text": "Title"},
            original_block,
            original_block.copy(),
        ]
        prepared = await prepare_thumbnail_blocks(blocks, self.downloader, 7)
        self.downloader.from_source.assert_awaited_once()
        self.assertEqual(prepared[0], blocks[0])
        self.assertEqual(prepared[1], prepared[2])
        self.assertNotIn("url", prepared[1])
        with PILImage.open(BytesIO(base64.b64decode(prepared[1]["data"]))) as image:
            self.assertEqual(image.format, "PNG")
        self.assertIn("url", original_block)

    async def test_safe_and_disabled_thumbnails_do_not_download(self):
        """Safe images, disabled processing and zero strength retain existing delivery."""
        for enabled, level, extra in (
            (True, 7, {"rating": "safe"}),
            (False, 7, {}),
            (True, 0, {}),
        ):
            with self.subTest(enabled=enabled, level=level):
                blocks = format_outcome(
                    SourceOutcome(
                        results=[
                            SearchResult(
                                "Title",
                                "Source",
                                "",
                                "https://example.com/t.png",
                                extra=extra,
                            )
                        ]
                    ),
                    nsfw_send_image=True,
                    thumbnail_mosaic=enabled,
                    max_results=1,
                    header="",
                )
                prepared = await prepare_thumbnail_blocks(
                    blocks, self.downloader, level
                )
                self.assertEqual(prepared, blocks)
        self.downloader.from_source.assert_not_awaited()

    async def test_failures_skip_images_and_keep_text_budget(self):
        """A failed download or decode never retries sending its original URL."""
        for download_error in (RuntimeError("Unavailable"), None):
            with self.subTest(download_error=download_error):
                self.downloader.from_source = AsyncMock(
                    side_effect=download_error,
                    return_value=SimpleNamespace(data=b"not an image"),
                )
                blocks = [
                    {"type": "text", "text": "Title and link"},
                    {
                        "type": "image",
                        "url": "https://example.com/t.png",
                        "mosaic": True,
                    },
                ]
                prepared = await prepare_thumbnail_blocks(
                    blocks, self.downloader, 7, 30
                )
                self.assertFalse(any(block["type"] == "image" for block in prepared))
                self.assertIn("Title and link", prepared[0]["text"])
                self.assertLessEqual(
                    sum(len(block["text"]) for block in prepared) + len(prepared) - 1,
                    30,
                )

    def test_processed_components_do_not_fall_back_to_remote_images(self):
        """Delivery uses base64 and fails closed if component creation is unavailable."""
        factory = MagicMock()
        factory.fromBase64.return_value = sentinel.processed
        with patch("astrbot.api.message_components.Image", factory):
            self.assertEqual(
                blocks_to_components([{"type": "image", "data": "encoded"}]),
                [sentinel.processed],
            )
            factory.fromBase64.side_effect = RuntimeError("Unavailable")
            self.assertEqual(
                blocks_to_components(
                    [
                        {
                            "type": "image",
                            "data": "encoded",
                            "url": "https://example.com/t.png",
                        }
                    ]
                ),
                [],
            )
        factory.fromURL.assert_not_called()

    def test_configuration_defaults_and_bounds(self):
        """Plugin configuration has safe defaults and clamps the slider range."""
        for configured, expected in ((None, 7), ("bad", 7), (-1, 0), (11, 10), (0, 0)):
            with self.subTest(configured=configured):
                config = (
                    {} if configured is None else {"thumbnail_mosaic_level": configured}
                )
                plugin = SoutuSearchPlugin(MagicMock(), config)
                self.assertTrue(plugin.thumbnail_mosaic)
                self.assertEqual(plugin.thumbnail_mosaic_level, expected)

    async def test_all_search_paths_process_unrated_thumbnails(self):
        """Book, keyword and multi-source handlers all send processed components."""
        outcome = SourceOutcome(
            results=[SearchResult("Title", "Source", "", "https://example.com/t.png")]
        )
        event = SimpleNamespace(chain_result=lambda components: components)
        payload = SimpleNamespace(data=b"query", filename="query.png", mime="image/png")
        for source in ("book", "keyword", "reverse"):
            with self.subTest(source=source):
                plugin = SoutuSearchPlugin(
                    MagicMock(),
                    {
                        "nsfw_send_image": True,
                        "thumbnail_mosaic": True,
                        "saucenao_api_key": "test-key",
                        "yandex_enable": False,
                        "ascii2d_enable": False,
                    },
                )
                plugin.image_source = self.downloader
                plugin.image_source.from_event = AsyncMock(return_value=payload)
                plugin.soutu.search = AsyncMock(return_value=outcome)
                plugin.booru.search_by_tags = AsyncMock(return_value=outcome)
                plugin.saucenao.search = AsyncMock(return_value=outcome)
                factory = MagicMock()
                factory.fromBase64.return_value = sentinel.processed
                with patch("astrbot.api.message_components.Image", factory):
                    if source == "book":
                        replies = [
                            reply
                            async for reply in plugin._search_by_image(event, payload)
                        ]
                    elif source == "keyword":
                        replies = [
                            reply
                            async for reply in plugin._search_by_keyword(event, "cat")
                        ]
                    else:
                        replies = [
                            reply async for reply in plugin._dispatch_saucenao(event)
                        ]
                self.assertEqual(len(replies), 1)
                self.assertIn(sentinel.processed, replies[0])
                factory.fromBase64.assert_called_once()
                factory.fromURL.assert_not_called()


if __name__ == "__main__":
    unittest.main()
