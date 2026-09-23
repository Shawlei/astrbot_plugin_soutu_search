"""astrbot_plugin_soutu_search 核心单元测试。

可在**不安装、不启动 AstrBot 本体**的情况下运行：在导入插件模块前，
用 ``unittest.mock`` 把 ``astrbot.*`` 相关模块塞进 ``sys.modules``。

覆盖点：
- soutubot 响应解析（正常 / 空 results / 字段缺失 / partial 状态）
- 相似度阈值与分档判定
- 标题回退链与链接优先级
- Safebooru 空结果与非 JSON 容错、评级过滤
- NSFW 关闭时不产生图片组件、且不泄露缩略图 URL
- TTL 缓存命中与过期
- 图片规范化（mime / data URI / file URI / 本地文件）
- 插件配置解析（含默认值）
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# --------------------------------------------------------------------------- #
# 路径准备
# --------------------------------------------------------------------------- #
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PARENT_DIR = PLUGIN_ROOT.parent
for _p in (str(PARENT_DIR), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --------------------------------------------------------------------------- #
# 构造 AstrBot 假模块
# --------------------------------------------------------------------------- #
mock_filter = MagicMock()
mock_filter.command = lambda *a, **k: (lambda fn: fn)
mock_filter.command_group = lambda *a, **k: (lambda fn: fn)
mock_filter.event_message_type = lambda *a, **k: (lambda fn: fn)
mock_filter.permission_type = lambda *a, **k: (lambda fn: fn)


class _EventMessageType:
    ALL = "ALL"


mock_filter.EventMessageType = _EventMessageType


class DummyStar:
    """AstrBot Star 基类的替身。"""

    def __init__(self, context=None):
        self.context = context


mock_astrbot = MagicMock()
mock_astrbot.api = MagicMock()
mock_astrbot.api.logger = MagicMock()
mock_astrbot.api.event = MagicMock()
mock_astrbot.api.event.AstrMessageEvent = MagicMock
mock_astrbot.api.event.filter = mock_filter
class _Plain:
    def __init__(self, text: str = ""):
        self.text = text


class _Image:
    def __init__(self, url: str = ""):
        self.url = url

    @classmethod
    def fromURL(cls, url):  # noqa: N802 - 对齐 AstrBot 命名
        return cls(url)


class _Node:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


mock_astrbot.api.message_components = MagicMock()
mock_astrbot.api.message_components.Image = _Image
mock_astrbot.api.message_components.Plain = _Plain
mock_astrbot.api.message_components.Node = _Node
mock_astrbot.api.star = MagicMock()
mock_astrbot.api.star.Context = MagicMock
mock_astrbot.api.star.Star = DummyStar
# StarTools 桩：**必须**让 get_data_dir 返回真实的（临时）绝对路径，
# 否则真正的 StarTools 是一个 MagicMock —— 而 ``isinstance(MagicMock(), os.PathLike)`` 为 True，
# 会诱导 _resolve_data_dir 执行 Path(candidate).mkdir()，在**工作目录**下生成
# ``MagicMock/mock/<id>`` 垃圾目录（每构造一次插件就多一个）。这里改为返回系统临时目录，
# 保证测试跑完后插件根目录与工作目录都保持干净（对应 P2-3）。
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="astrbot_soutu_testdata_")
mock_astrbot.api.star.StarTools = MagicMock()
mock_astrbot.api.star.StarTools.get_data_dir = lambda *a, **k: _TEST_DATA_DIR

sys.modules["astrbot"] = mock_astrbot
sys.modules["astrbot.api"] = mock_astrbot.api
sys.modules["astrbot.api.logger"] = mock_astrbot.api.logger
sys.modules["astrbot.api.event"] = mock_astrbot.api.event
sys.modules["astrbot.api.message_components"] = mock_astrbot.api.message_components
sys.modules["astrbot.api.star"] = mock_astrbot.api.star

# --------------------------------------------------------------------------- #
# 导入被测模块
# --------------------------------------------------------------------------- #
from astrbot.api.message_components import Plain  # noqa: E402  (mocked)

from astrbot_plugin_soutu_search.core.cache import (  # noqa: E402
    TTLCache,
    make_image_key,
    make_tags_key,
    sha256_hex,
)
from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    blocks_to_components,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.image_source import (  # noqa: E402
    ImageSource,
    classify_source,
    guess_ext,
    guess_mime,
    is_data_uri,
    parse_data_uri,
    strip_file_uri,
)
from astrbot_plugin_soutu_search.core.safebooru_client import (  # noqa: E402
    build_safebooru_url,
    parse_safebooru_response,
)
from astrbot_plugin_soutu_search.core.soutu_client import (  # noqa: E402
    CONFIDENCE_THRESHOLD_NORMAL,
    CONFIDENCE_THRESHOLD_STRICT,
    MAIN_TIER_THRESHOLD,
    SOURCE_NAME_MAP,
    extract_title,
    extract_url,
    parse_soutu_response,
    resolve_threshold,
    source_display_name,
)
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    HELP_TEXT,
    SoutuSearchPlugin,
    _to_bool,
    _to_int,
    _to_str,
)

# --------------------------------------------------------------------------- #
# 样例构造工具
# --------------------------------------------------------------------------- #


def make_segment(**overrides) -> dict:
    seg = {
        "source_key": "nhentai",
        "external_id": "443172",
        "page_no": 212,
        "thumbnail_url": "https://img.example/t.jpg",
        "source_url": "https://nhentai.net/g/443172",
        "page_url": "https://nhentai.net/g/443172/212/",
        "metadata": {
            "display_kind": "doujinshi",
            "source": {"key": "nhentai", "url": "https://nhentai.net/g/443172"},
            "title": {"primary": "Primary Title", "japanese_or_alias": "[JP] Title"},
        },
    }
    seg.update(overrides)
    return seg


def make_result(score, segments) -> dict:
    return {"hit_id": "h", "score": score, "path_segments": segments}


# --------------------------------------------------------------------------- #
# soutubot 解析
# --------------------------------------------------------------------------- #
class TestSoutuParsing(unittest.TestCase):
    def test_normal(self):
        payload = {
            "schema_version": "2.2",
            "status": "ok",
            "partial": False,
            "results": [make_result(84.27, [make_segment()])],
        }
        outcome = parse_soutu_response(payload, factor="1.2", min_score=28)
        self.assertEqual(len(outcome.results), 1)
        r = outcome.results[0]
        self.assertEqual(r.title, "Primary Title")
        self.assertEqual(r.url, "https://nhentai.net/g/443172/212/")
        self.assertEqual(r.source, "NH本子")
        self.assertAlmostEqual(r.score, 84.27, places=2)
        self.assertFalse(r.extra["low_confidence"])
        self.assertEqual(r.extra["tier"], "main")
        self.assertEqual(r.extra["page_no"], 212)

    def test_empty_results(self):
        outcome = parse_soutu_response({"results": []}, "1.2", 28)
        self.assertEqual(outcome.results, [])
        self.assertEqual(outcome.meta["hit_count"], 0)

    def test_missing_fields_no_keyerror(self):
        # 仅有 source_key，其余字段全缺
        payload = {"results": [{"path_segments": [{"source_key": "jmcomic"}]}]}
        outcome = parse_soutu_response(payload, "1.2", 28)
        self.assertEqual(len(outcome.results), 1)
        r = outcome.results[0]
        self.assertEqual(r.title, "")
        self.assertEqual(r.url, "")
        self.assertIsNone(r.score)
        self.assertEqual(r.source, "禁漫")
        self.assertIsNone(r.thumbnail)

    def test_segments_empty_means_no_hit(self):
        payload = {"results": [{"score": 90, "path_segments": []}, {"score": 90}]}
        outcome = parse_soutu_response(payload, "1.2", 0)
        self.assertEqual(outcome.results, [])
        self.assertEqual(outcome.meta["hit_count"], 0)

    def test_partial_flag(self):
        payload = {
            "status": "partial",
            "partial": True,
            "results": [make_result(90.0, [make_segment()])],
        }
        outcome = parse_soutu_response(payload, "1.2", 28)
        self.assertTrue(outcome.meta["partial"])
        self.assertTrue(any("partial" in w.lower() for w in outcome.warnings))

    def test_invalid_payload(self):
        for bad in (None, [], "text", 123):
            outcome = parse_soutu_response(bad, "1.2", 28)
            self.assertEqual(outcome.results, [])
            self.assertTrue(outcome.warnings)

    def test_multiple_segments(self):
        payload = {"results": [make_result(60.0, [make_segment(), make_segment(source_key="ehentai")])]}
        outcome = parse_soutu_response(payload, "1.2", 28)
        self.assertEqual(len(outcome.results), 2)
        self.assertEqual(outcome.meta["hit_count"], 1)

    def test_threshold_values(self):
        self.assertEqual(resolve_threshold("1.4"), CONFIDENCE_THRESHOLD_STRICT)
        self.assertEqual(resolve_threshold("1.2"), CONFIDENCE_THRESHOLD_NORMAL)
        self.assertEqual(resolve_threshold(1.4), 35.0)
        self.assertEqual(MAIN_TIER_THRESHOLD, 28.0)

    def test_low_confidence_and_tier(self):
        payload = {"results": [make_result(40.0, [make_segment()])]}
        # factor 1.2 阈值 45 → 40 属低置信度，但 >=28 进主列表
        o1 = parse_soutu_response(payload, "1.2", 28)
        self.assertTrue(o1.results[0].extra["low_confidence"])
        self.assertEqual(o1.results[0].extra["tier"], "main")
        # factor 1.4 阈值 35 → 40 不算低置信度
        o2 = parse_soutu_response(payload, "1.4", 28)
        self.assertFalse(o2.results[0].extra["low_confidence"])

        # 低分档：20 分
        low = {"results": [make_result(20.0, [make_segment()])]}
        o3 = parse_soutu_response(low, "1.2", 0)  # min_score=0 保留
        self.assertEqual(o3.results[0].extra["tier"], "low")
        o4 = parse_soutu_response(low, "1.2", 28)  # 默认 min_score 过滤掉
        self.assertEqual(o4.results, [])

    def test_title_fallback_chain(self):
        # 平铺字符串
        seg = make_segment(metadata={"title": "Flat Title", "source": {"key": "nhentai"}})
        o = parse_soutu_response({"results": [make_result(50.0, [seg])]}, "1.2", 28)
        self.assertEqual(o.results[0].title, "Flat Title")
        # 仅 japanese_or_alias
        seg = make_segment(metadata={"title": {"japanese_or_alias": "[JP] Alias"}, "source": {"key": "nhentai"}})
        o = parse_soutu_response({"results": [make_result(50.0, [seg])]}, "1.2", 28)
        self.assertEqual(o.results[0].title, "[JP] Alias")
        # 完全缺失
        seg = make_segment(metadata={})
        o = parse_soutu_response({"results": [make_result(50.0, [seg])]}, "1.2", 28)
        self.assertEqual(o.results[0].title, "")
        # 纯函数直测
        self.assertEqual(extract_title({"title": {"primary": "P", "japanese_or_alias": "A"}}), "P")
        self.assertEqual(extract_title({"title": "S"}), "S")
        self.assertEqual(extract_title({}), "")

    def test_url_priority(self):
        self.assertEqual(
            extract_url({"page_url": "P", "chapter_url": "C", "source_url": "S"}, {"source": {"url": "M"}}),
            "P",
        )
        self.assertEqual(extract_url({"chapter_url": "C", "source_url": "S"}, {"source": {"url": "M"}}), "C")
        self.assertEqual(extract_url({"source_url": "S"}, {"source": {"url": "M"}}), "S")
        self.assertEqual(extract_url({}, {"source": {"url": "M"}}), "M")
        self.assertEqual(extract_url({}, {}), "")

    def test_source_display_name(self):
        self.assertEqual(source_display_name("nhentai"), "NH本子")
        self.assertEqual(source_display_name("ehentai"), "E站")
        self.assertEqual(source_display_name("pixiv"), "Pixiv")
        self.assertEqual(source_display_name("weird_site"), "weird_site")
        self.assertEqual(source_display_name(None), "未知来源")
        for key in ("nhentai", "ehentai", "jmcomic", "manhuacat", "gelbooru", "yande", "panda", "zerochan", "pixiv"):
            self.assertIn(key, SOURCE_NAME_MAP)


# --------------------------------------------------------------------------- #
# Safebooru 解析
# --------------------------------------------------------------------------- #
class TestSafebooruParsing(unittest.TestCase):
    def test_empty_and_invalid(self):
        for text in ("", "   ", "[]"):
            outcome = parse_safebooru_response(text, rating_filter="safe")
            self.assertEqual(outcome.results, [])
        # 非 JSON 容错：不能抛异常
        outcome = parse_safebooru_response("<html>rate limited</html>", rating_filter="safe")
        self.assertEqual(outcome.results, [])
        self.assertTrue(outcome.warnings)

    def test_parse_and_rating_filter(self):
        posts = [
            {"id": 1, "tags": "cat_ears blue_eyes", "preview_url": "p1", "file_url": "f1",
             "width": 100, "height": 200, "rating": "general", "score": 5},
            {"id": 2, "tags": "x", "preview_url": "p2", "rating": "explicit"},
            {"id": 3, "tags": "y", "preview_url": "p3", "rating": "questionable"},
        ]
        safe = parse_safebooru_response(json.dumps(posts), base_url="https://safebooru.org", rating_filter="safe")
        self.assertEqual(len(safe.results), 1)
        r = safe.results[0]
        self.assertEqual(r.url, "https://safebooru.org/index.php?page=post&s=view&id=1")
        self.assertEqual(r.thumbnail, "p1")
        self.assertIn("cat_ears", r.title)
        self.assertEqual(r.source, "Safebooru")
        self.assertEqual(r.extra["rating"], "general")
        self.assertEqual(r.extra["width"], 100)

        all_rating = parse_safebooru_response(json.dumps(posts), rating_filter="all")
        self.assertEqual(len(all_rating.results), 3)

    def test_view_url_without_id(self):
        posts = [{"tags": "z", "rating": "general"}]
        outcome = parse_safebooru_response(json.dumps(posts), base_url="https://safebooru.org/", rating_filter="safe")
        self.assertEqual(outcome.results[0].url, "https://safebooru.org")
        self.assertIsNone(outcome.results[0].thumbnail)

    def test_build_url(self):
        url = build_safebooru_url("https://safebooru.org", "cat ears", limit=5, page=1)
        self.assertIn("tags=cat+ears", url)
        self.assertIn("limit=5", url)
        self.assertIn("pid=1", url)
        self.assertIn("json=1", url)
        self.assertIn("q=index", url)


# --------------------------------------------------------------------------- #
# 格式化（NSFW 硬性验收点）
# --------------------------------------------------------------------------- #
class TestFormatter(unittest.TestCase):
    def _make_outcome(self, thumbnail="https://thumb/1.jpg"):
        return SourceOutcome(
            results=[
                SearchResult(
                    title="T",
                    source="NH本子",
                    url="https://site/detail/1",
                    thumbnail=thumbnail,
                    score=90.0,
                    extra={"page_no": 5},
                )
            ]
        )

    def test_no_image_when_nsfw_off(self):
        blocks = format_outcome(self._make_outcome(), nsfw_send_image=False, max_results=3, header="H")
        self.assertTrue(all(b["type"] == "text" for b in blocks))
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertNotIn("thumb/1.jpg", joined)  # 缩略图 URL 绝不泄露
        self.assertIn("site/detail/1", joined)  # 但来源链接必须给出

    def test_image_when_nsfw_on(self):
        blocks = format_outcome(self._make_outcome(), nsfw_send_image=True, max_results=3, header="H")
        self.assertTrue(any(b["type"] == "image" and b["url"] == "https://thumb/1.jpg" for b in blocks))

    def test_blocks_to_components_off(self):
        blocks = format_outcome(self._make_outcome(), nsfw_send_image=False, max_results=3, header="H")
        comps = blocks_to_components(blocks)
        self.assertEqual(len(comps), len(blocks))
        self.assertTrue(all(isinstance(c, Plain) for c in comps))

    def test_blocks_to_components_on(self):
        blocks = format_outcome(self._make_outcome(), nsfw_send_image=True, max_results=3, header="H")
        comps = blocks_to_components(blocks)
        # 至少有一个非 Plain 组件（Image）
        self.assertTrue(any(not isinstance(c, Plain) for c in comps))

    def test_truncation_and_low_confidence_note(self):
        results = [
            SearchResult(title=f"T{i}", source="S", url=f"https://a/{i}", score=50.0, extra={})
            for i in range(5)
        ]
        outcome = SourceOutcome(results=results, warnings=["低置信度示例"])
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="H")
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertIn("仅展示前 3 条", joined)
        self.assertIn("低置信度示例", joined)

    def test_empty_outcome(self):
        blocks = format_outcome(SourceOutcome(), nsfw_send_image=False, max_results=3, header="H")
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertIn("没有找到", joined)


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
class TestCache(unittest.TestCase):
    def test_hit_and_expire(self):
        cache = TTLCache(default_ttl=60)
        cache.set("a", 123)
        self.assertEqual(cache.get("a"), 123)
        cache.set("fast", "v", ttl=0.05)
        time.sleep(0.12)
        self.assertIsNone(cache.get("fast"))
        self.assertIsNone(cache.get("missing"))

    def test_ttl_zero_disables(self):
        cache = TTLCache(default_ttl=60)
        cache.set("off", "x", ttl=0)
        self.assertIsNone(cache.get("off"))

    def test_make_keys(self):
        self.assertEqual(make_image_key(b"abc"), make_image_key(b"abc"))
        self.assertNotEqual(make_image_key(b"abc"), make_image_key(b"abd"))
        self.assertTrue(make_image_key(b"abc").startswith("img:"))
        self.assertEqual(sha256_hex(b"abc"), sha256_hex(b"abc"))
        self.assertEqual(make_tags_key("Cat  Ears"), make_tags_key("cat ears"))

    def test_prune_and_clear(self):
        cache = TTLCache(default_ttl=60)
        cache.set("a", 1)
        cache.set("b", 2, ttl=0.01)
        time.sleep(0.05)
        # 过期项在 get 时惰性删除；prune 主动清理
        self.assertGreaterEqual(cache.prune(), 0)
        cache.clear()
        self.assertEqual(len(cache), 0)


# --------------------------------------------------------------------------- #
# 图片规范化
# --------------------------------------------------------------------------- #
class TestImageSource(unittest.TestCase):
    def test_guess_mime_and_ext(self):
        self.assertEqual(guess_mime(b"\x89PNG\r\n\x1a\n...."), "image/png")
        self.assertEqual(guess_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(guess_mime(b"RIFF....WEBP"), "image/webp")
        self.assertEqual(guess_mime(b"GIF89a"), "image/gif")
        self.assertEqual(guess_ext("image/png"), "png")
        self.assertEqual(guess_ext("image/jpeg"), "jpg")
        self.assertEqual(guess_ext("image/webp"), "webp")

    def test_classify_source(self):
        self.assertEqual(classify_source("http://a/b.jpg"), "url")
        self.assertEqual(classify_source("https://a/b.jpg"), "url")
        self.assertEqual(classify_source("data:image/png;base64,AAAA"), "data")
        self.assertEqual(classify_source("C:/a/b.jpg"), "file")
        self.assertEqual(classify_source(""), "unknown")

    def test_parse_data_uri(self):
        raw = b"hello-bytes"
        uri = "data:image/png;base64," + base64.b64encode(raw).decode()
        self.assertTrue(is_data_uri(uri))
        data, mime = parse_data_uri(uri)
        self.assertEqual(data, raw)
        self.assertEqual(mime, "image/png")

    def test_strip_file_uri(self):
        path = strip_file_uri("file:///C:/a/b.jpg")
        self.assertTrue(path.replace("\\", "/").endswith("a/b.jpg"))

    def test_from_source_data_uri(self):
        raw = b"\x89PNG\r\n\x1a\nDATA"

        async def run():
            source = ImageSource(timeout=5)
            try:
                return await source.from_source("data:image/png;base64," + base64.b64encode(raw).decode())
            finally:
                await source.close()

        payload = asyncio_run(run())
        self.assertEqual(payload.data, raw)
        self.assertEqual(payload.mime, "image/png")
        self.assertEqual(payload.source_kind, "data")

    def test_from_source_local_file(self):
        tmp_dir = Path(__file__).resolve().parent / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / "t.png"
        raw = b"\x89PNG\r\n\x1a\nLOCALDATA"
        target.write_bytes(raw)
        try:

            async def run():
                source = ImageSource(timeout=5, allowed_roots=[tmp_dir])
                try:
                    return await source.from_source(str(target))
                finally:
                    await source.close()

            payload = asyncio_run(run())
            self.assertEqual(payload.data, raw)
            self.assertEqual(payload.mime, "image/png")
            self.assertEqual(payload.source_kind, "file")
            self.assertEqual(payload.filename, "t.png")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_from_source_local_missing(self):
        async def run():
            source = ImageSource(timeout=5)
            try:
                await source.from_source(str(Path(__file__).parent / "not_exist_xyz.png"))
            finally:
                await source.close()

        with self.assertRaises(FileNotFoundError):
            asyncio_run(run())


# --------------------------------------------------------------------------- #
# 插件配置
# --------------------------------------------------------------------------- #
class TestPluginConfig(unittest.TestCase):
    def test_defaults(self):
        plugin = SoutuSearchPlugin(MagicMock(), {})
        self.assertTrue(plugin.enable_auto_search)
        self.assertFalse(plugin.nsfw_send_image)  # NSFW 默认关闭（硬性）
        self.assertEqual(plugin.result_count, 3)
        self.assertEqual(plugin.min_score, 28)
        self.assertEqual(plugin.search_factor, "1.2")
        self.assertEqual(plugin.safebooru_rating, "safe")
        self.assertEqual(plugin.auto_search_cooldown, 30)
        self.assertEqual(plugin.cache_ttl, 3600)

    def test_overrides_and_fallbacks(self):
        plugin = SoutuSearchPlugin(
            MagicMock(),
            {
                "nsfw_send_image": True,
                "result_count": 5,
                "search_factor": "1.4",
                "safebooru_rating": "all",
                "enable_auto_search": False,
            },
        )
        self.assertTrue(plugin.nsfw_send_image)
        self.assertEqual(plugin.result_count, 5)
        self.assertEqual(plugin.search_factor, "1.4")
        self.assertEqual(plugin.safebooru_rating, "all")
        self.assertFalse(plugin.enable_auto_search)

        # 非法值回退
        plugin2 = SoutuSearchPlugin(MagicMock(), {"search_factor": "9.9", "safebooru_rating": "xxx"})
        self.assertEqual(plugin2.search_factor, "1.2")
        self.assertEqual(plugin2.safebooru_rating, "safe")

    def test_config_helpers(self):
        self.assertTrue(_to_bool("true", False))
        self.assertTrue(_to_bool(1, False))
        self.assertFalse(_to_bool("nope", True))
        self.assertTrue(_to_bool(None, True))
        self.assertEqual(_to_int("7", 0), 7)
        self.assertEqual(_to_int("bad", 3), 3)
        self.assertEqual(_to_str("", "d"), "d")
        self.assertEqual(_to_str("  x  ", "d"), "x")

    def test_help_text_exists(self):
        self.assertIn("/搜图", HELP_TEXT)


def asyncio_run(coro):
    """运行协程（延迟导入 asyncio，保持文件顶部整洁）。"""
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
