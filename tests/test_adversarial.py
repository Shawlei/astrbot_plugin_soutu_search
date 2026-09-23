"""QA 对抗性 / 边界测试（独立于工程师的 test_core.py）。

设计目标：证伪工程师的「全绿」结论，逐条覆盖任务书要求的必测项。
复用 tests/test_core.py 的 astrbot 桩（import 即完成 sys.modules 装配）。

运行::
    python -m unittest tests.test_adversarial -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search.core.cache import TTLCache, make_image_key  # noqa: E402
from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.image_source import (  # noqa: E402
    ImagePayload,
    ImageSource,
    guess_ext,
    parse_data_uri,
)
from astrbot_plugin_soutu_search.core.safebooru_client import parse_safebooru_response  # noqa: E402
from astrbot_plugin_soutu_search.core.soutu_client import (  # noqa: E402
    SOURCE_NAME_MAP,
    extract_title,
    extract_url,
    parse_soutu_response,
    resolve_threshold,
)
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    SoutuSearchPlugin,
    _is_command_message,
)

RECON = PLUGIN_ROOT.parent / "recon"
CONF_SCHEMA = PLUGIN_ROOT / "_conf_schema.json"
REAL_PAYLOAD = json.loads((RECON / "search_resp.json").read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


def seg(**kw) -> dict:
    base = {
        "source_key": "nhentai",
        "page_no": 1,
        "thumbnail_url": "https://thumb/x.jpg",
        "page_url": "https://page/1",
        "metadata": {"title": {"primary": "P", "japanese_or_alias": "A"},
                     "source": {"key": "nhentai", "url": "https://meta/1"}},
    }
    base.update(kw)
    return base


def res(score, segments):
    return {"score": score, "path_segments": segments}


# ===========================================================================
# C. 真实响应样例解析
# ===========================================================================
class TestRealPayload(unittest.TestCase):
    def test_all_segments_extracted(self):
        o = parse_soutu_response(REAL_PAYLOAD, factor="1.2", min_score=0)
        self.assertEqual(o.meta["hit_count"], 75, "应为 75 条命中")
        self.assertEqual(o.meta["total_segments"], 78, "75 条结果共 78 个 segment")
        self.assertEqual(len(o.results), 78)

    def test_score_precision_preserved(self):
        o = parse_soutu_response(REAL_PAYLOAD, factor="1.2", min_score=0)
        top = o.results[0]
        self.assertEqual(top.score, 84.273575, "分数必须保留原精度，不得四舍五入")

    def test_default_min_score_filters_to_main_tier(self):
        o = parse_soutu_response(REAL_PAYLOAD, factor="1.2")  # min_score=28
        self.assertEqual(len(o.results), 2, ">=28 的实测仅 2 条")

    def test_source_keys_all_mapped(self):
        # 实测出现的 9 种 source_key 必须在映射表内
        keys = {"nhentai", "ehentai", "jmcomic", "manhuacat", "gelbooru",
                "yande", "panda", "zerochan", "pixiv"}
        missing = keys - set(SOURCE_NAME_MAP)
        self.assertEqual(missing, set(), f"中文映射缺失: {missing}")
        o = parse_soutu_response(REAL_PAYLOAD, factor="1.2", min_score=0)
        for r in o.results:
            self.assertNotIn(r.source, ("", "未知来源"), f"source_key 未被映射: {r.extra['source_key']}")

    def test_manhuacat_url_empty_is_graceful(self):
        # 实测有 manhuacat 结果无任何链接 → url 应为空串且不崩
        o = parse_soutu_response(REAL_PAYLOAD, factor="1.2", min_score=0)
        empty = [r for r in o.results if not r.url]
        # 记录数量（不作为失败条件，但需证明未抛异常）
        self.assertIsInstance(empty, list)


# ===========================================================================
# B. 标题回退链 / 链接优先级 / 阈值 / 分档
# ===========================================================================
class TestFallbacks(unittest.TestCase):
    def test_title_chain_three_variants(self):
        # 变体1：结构化 primary
        self.assertEqual(extract_title({"title": {"primary": "PR", "japanese_or_alias": "JA"}}), "PR")
        # 变体2：平铺字符串
        self.assertEqual(extract_title({"title": "FLAT"}), "FLAT")
        # 变体3：仅 japanese_or_alias
        self.assertEqual(extract_title({"title": {"japanese_or_alias": "JA"}}), "JA")
        # 边界：primary 为空串 → 回退 alias
        self.assertEqual(extract_title({"title": {"primary": "  ", "japanese_or_alias": "JA"}}), "JA")
        # metadata 非 dict
        self.assertEqual(extract_title(None), "")
        self.assertEqual(extract_title("x"), "")  # type: ignore[arg-type]

    def test_url_priority_full_chain(self):
        self.assertEqual(extract_url({"page_url": "P", "chapter_url": "C", "source_url": "S"},
                                     {"source": {"url": "M"}}), "P")
        self.assertEqual(extract_url({"chapter_url": "C", "source_url": "S"},
                                     {"source": {"url": "M"}}), "C")
        self.assertEqual(extract_url({"source_url": "S"}, {"source": {"url": "M"}}), "S")
        self.assertEqual(extract_url({}, {"source": {"url": "M"}}), "M")
        self.assertEqual(extract_url({}, {}), "")
        # 空串应被跳过
        self.assertEqual(extract_url({"page_url": "  ", "source_url": "S"}, {}), "S")

    def test_threshold_both_branches(self):
        self.assertEqual(resolve_threshold("1.4"), 35.0)
        self.assertEqual(resolve_threshold(1.4), 35.0)
        self.assertEqual(resolve_threshold("1.2"), 45.0)
        self.assertEqual(resolve_threshold("2.0"), 45.0)
        # 解析结果中的 threshold 元数据
        o14 = parse_soutu_response({"results": []}, "1.4")
        o12 = parse_soutu_response({"results": []}, "1.2")
        self.assertEqual(o14.meta["threshold"], 35.0)
        self.assertEqual(o12.meta["threshold"], 45.0)

    def test_tier_boundary_27_99_28_0_28_01(self):
        for score, expect in ((27.99, "low"), (28.0, "main"), (28.01, "main")):
            o = parse_soutu_response({"results": [res(score, [seg()])]}, "1.2", min_score=0)
            self.assertEqual(o.results[0].extra["tier"], expect, f"score={score}")
        # 27.99 在默认 min_score=28 下应被过滤
        o = parse_soutu_response({"results": [res(27.99, [seg()])]}, "1.2", min_score=28)
        self.assertEqual(o.results, [])


# ===========================================================================
# D. 破坏性容错
# ===========================================================================
class TestDestructive(unittest.TestCase):
    def test_results_empty_null_missing(self):
        for payload in ({"results": []}, {"results": None}, {}, {"results": "x"}):
            o = parse_soutu_response(payload, "1.2", 28)
            self.assertEqual(o.results, [])
            self.assertEqual(o.meta["hit_count"], 0)

    def test_path_segments_variants(self):
        for segs in ([], None, "x", [None, 5, "str"]):
            o = parse_soutu_response({"results": [{"score": 90, "path_segments": segs}]}, "1.2", 0)
            self.assertEqual(o.results, [], f"segments={segs!r} 不应产生命中")
        # 缺失 path_segments 键
        o = parse_soutu_response({"results": [{"score": 90}]}, "1.2", 0)
        self.assertEqual(o.results, [])

    def test_score_weird_types(self):
        cases = {"84.2": 84.2, None: None, -5: -5.0, 200: 200.0, "abc": None, True: 1.0}
        for raw, expect in cases.items():
            o = parse_soutu_response({"results": [res(raw, [seg()])]}, "1.2", min_score=-999)
            self.assertEqual(o.results[0].score, expect, f"score={raw!r}")

    def test_metadata_variants(self):
        for md in (None, "str", 123, []):
            o = parse_soutu_response({"results": [{"score": 50, "path_segments": [{"source_key": "nhentai", "metadata": md}]}]}, "1.2", 0)
            self.assertEqual(len(o.results), 1)
            self.assertEqual(o.results[0].title, "")
        # title 为字符串
        o = parse_soutu_response({"results": [{"score": 50, "path_segments": [{"metadata": {"title": "T"}}]}]}, "1.2", 0)
        self.assertEqual(o.results[0].title, "T")

    def test_status_partial(self):
        for payload in ({"status": "partial", "results": []},
                        {"partial": True, "results": []}):
            o = parse_soutu_response(payload, "1.2", 28)
            self.assertTrue(o.meta["partial"])
            self.assertTrue(any("partial" in w.lower() for w in o.warnings))

    def test_payload_non_dict(self):
        for bad in (None, [], "text", 1, True):
            o = parse_soutu_response(bad, "1.2", 28)
            self.assertEqual(o.results, [])
            self.assertTrue(o.warnings)

    def test_safebooru_destructive(self):
        self.assertEqual(parse_safebooru_response("").results, [])
        self.assertEqual(parse_safebooru_response("   ").results, [])
        self.assertEqual(parse_safebooru_response("[]").results, [])
        self.assertEqual(parse_safebooru_response("<html>Just a moment...</html>").results, [])
        # 单条对象而非数组
        o = parse_safebooru_response('{"id": 1, "tags": "x"}')
        self.assertEqual(o.results, [])
        self.assertTrue(o.warnings)
        # null 文本
        self.assertEqual(parse_safebooru_response(None).results, [])  # type: ignore[arg-type]

    def test_soutu_client_empty_image_raises_expected(self):
        from astrbot_plugin_soutu_search.core.soutu_client import SoutuClient

        async def go():
            c = SoutuClient(base_url="https://soutubot.moe", timeout=30)
            try:
                await c.search(b"", filename="q.jpg", mime="image/jpeg")
            finally:
                await c.close()

        with self.assertRaises(ValueError):
            run(go())


# ===========================================================================
# 图片输入破坏性
# ===========================================================================
class TestImageDestructive(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qa_img_", dir=str(PLUGIN_ROOT / "tests")))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zero_byte_file(self):
        p = self.tmp / "empty.png"
        p.write_bytes(b"")

        async def go():
            s = ImageSource(timeout=5)
            try:
                return await s.from_source(str(p))
            finally:
                await s.close()

        with self.assertRaises(Exception):  # RuntimeError("本地图片为空")
            run(go())

    def test_corrupt_file_rejected_by_magic(self):
        """[工程师已修复 #7] 非图片字节必须被魔数校验拒绝，而非原样接受。"""
        p = self.tmp / "corrupt.bin"
        p.write_bytes(b"\x00\x01\x02not-an-image")

        async def go():
            s = ImageSource(timeout=5, allowed_roots=[self.tmp])
            try:
                return await s.from_source(str(p))
            finally:
                await s.close()

        with self.assertRaises(RuntimeError):
            run(go())

    def test_data_uri_without_base64(self):
        uri = "data:image/png," + "hello%20world"
        data, mime = parse_data_uri(uri)
        self.assertEqual(data, b"hello world")
        self.assertEqual(mime, "image/png")
        # 无内容应抛 ValueError
        with self.assertRaises(ValueError):
            parse_data_uri("data:image/png,")

    def test_file_uri_nonexistent(self):
        async def go():
            s = ImageSource(timeout=5)
            try:
                return await s.from_source("file:///Z:/no/such/file_xyz.png")
            finally:
                await s.close()

        with self.assertRaises(FileNotFoundError):
            run(go())

    def test_non_image_mime_data_uri(self):
        uri = "data:text/plain;base64," + base64.b64encode(b"hello").decode()
        data, mime = parse_data_uri(uri)
        self.assertEqual(mime, "text/plain")
        self.assertEqual(guess_ext(mime), "jpg")  # 未知 MIME 回退 jpg（记录：无类型校验）

    def test_local_file_read_via_component_blocked(self):
        """[工程师已修复 #6] 组件 file 指向白名单外本地路径时，必须被拒绝。"""
        secret = self.tmp / "secret.png"
        secret.write_bytes(b"\x89PNG\r\n\x1a\nSECRETDATA")

        class Image:  # 名称需为 Image 以通过鸭子类型判定
            def __init__(self, f):
                self.file = str(f)

        class MsgObj:
            def __init__(self, comps):
                self.message = comps

        class Ev:
            def __init__(self, comps):
                self.message_obj = MsgObj(comps)

        async def go():
            s = ImageSource(timeout=5)  # 未配置 allowed_roots → 拒绝一切本地文件
            try:
                return await s.from_event(Ev([Image(secret)]))
            finally:
                await s.close()

        payload = run(go())
        self.assertIsNone(payload, "白名单外本地文件必须被拒绝（from_event 吞异常返回 None）")


# ===========================================================================
# E. NSFW 硬性验收点
# ===========================================================================
class TestNsfwHardGate(unittest.TestCase):
    def _mk_outcome(self, thumbs):
        return SourceOutcome(results=[
            SearchResult(title=f"T{i}", source="S", url=f"https://detail/{i}",
                         thumbnail=t, score=90.0, extra={})
            for i, t in enumerate(thumbs)
        ])

    def test_schema_default_false(self):
        schema = json.loads(CONF_SCHEMA.read_text(encoding="utf-8"))
        self.assertIs(schema["nsfw_send_image"]["default"], False)

    def test_off_no_image_and_no_thumbnail_leak(self):
        thumbs = ["https://cdn.example/secretA.jpg", "https://cdn.example/secretB.png"]
        blocks = format_outcome(self._mk_outcome(thumbs), nsfw_send_image=False,
                                max_results=5, header="H")
        # 1) 不得含任何 image 块
        self.assertTrue(all(b["type"] == "text" for b in blocks))
        # 2) 文本中不得出现任何缩略图 URL（含子串）
        joined = "\n".join(b.get("text", "") for b in blocks)
        for t in thumbs:
            self.assertNotIn(t, joined)
            self.assertNotIn("secretA", joined)
        # 3) 详情链接必须仍在
        self.assertIn("detail/0", joined)

    def test_on_produces_image_blocks(self):
        thumbs = ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]
        blocks = format_outcome(self._mk_outcome(thumbs), nsfw_send_image=True,
                                max_results=5, header="H")
        imgs = [b for b in blocks if b["type"] == "image"]
        self.assertEqual(len(imgs), 2)
        self.assertEqual({b["url"] for b in imgs}, set(thumbs))

    def test_plugin_default_off(self):
        p = SoutuSearchPlugin(object(), {})
        self.assertFalse(p.nsfw_send_image)


# ===========================================================================
# F. 配置一致性 / 容错 / 依赖 / 入口
# ===========================================================================
class TestConfigAndSpec(unittest.TestCase):
    def test_schema_vs_code_keys(self):
        schema = set(json.loads(CONF_SCHEMA.read_text(encoding="utf-8")).keys())
        src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        used = set(re.findall(r'cfg\.get\(\s*"([^"]+)"', src))
        self.assertEqual(used - schema, set(), f"用了但未在 schema 定义: {used - schema}")
        self.assertEqual(schema - used, set(), f"定义了但未使用: {schema - used}")

    def test_config_fallbacks(self):
        p = SoutuSearchPlugin(object(), {
            "search_factor": "9.9",
            "safebooru_rating": "xxx",
            "result_count": -1,
            "cache_ttl": "not-int",
            "min_score": -5,
            "request_timeout": 0,
        })
        self.assertEqual(p.search_factor, "1.2")
        self.assertEqual(p.safebooru_rating, "safe")
        self.assertGreaterEqual(p.result_count, 1)
        self.assertGreaterEqual(p.cache_ttl, 0)
        self.assertGreaterEqual(p.min_score, 0)
        self.assertGreaterEqual(p.request_timeout, 5)

    def test_config_missing_all(self):
        p = SoutuSearchPlugin(object(), {})
        self.assertEqual(p.result_count, 3)
        self.assertEqual(p.cache_ttl, 3600)
        self.assertEqual(p.auto_search_cooldown, 30)

    def test_no_requests_dependency(self):
        glob = list(PLUGIN_ROOT.rglob("*.py"))
        offenders = []
        for f in glob:
            text = f.read_text(encoding="utf-8")
            if re.search(r"^\s*import\s+requests\b", text, re.M) or \
               re.search(r"^\s*from\s+requests\b", text, re.M):
                offenders.append(str(f))
        self.assertEqual(offenders, [], f"存在 requests 依赖: {offenders}")

    def test_entry_class_signature(self):
        import inspect
        sig = inspect.signature(SoutuSearchPlugin.__init__)
        params = list(sig.parameters)
        self.assertEqual(params[0], "self")
        self.assertEqual(params[1], "context")
        self.assertTrue((PLUGIN_ROOT / "main.py").exists())
        from astrbot_plugin_soutu_search.main import SoutuSearchPlugin as P
        self.assertTrue(issubclass(P, _stub.DummyStar))

    def test_terminate_idempotent(self):
        p = SoutuSearchPlugin(object(), {})

        async def go():
            await p.terminate()
            await p.terminate()  # 二次关闭不应报错

        run(go())


# ===========================================================================
# 缓存 / 冷却 / 去重
# ===========================================================================
class TestCacheAndCooldown(unittest.TestCase):
    def test_ttl_expiry(self):
        c = TTLCache(default_ttl=100)
        c.set("k", "v", ttl=0.05)
        self.assertEqual(c.get("k"), "v")
        time.sleep(0.08)
        self.assertIsNone(c.get("k"))

    def test_ttl_zero_disables(self):
        c = TTLCache(default_ttl=0)
        c.set("k", "v")
        self.assertIsNone(c.get("k"))

    def test_contains(self):
        c = TTLCache(default_ttl=100)
        c.set("k", "v")
        self.assertIn("k", c)

    def test_concurrent_same_image_key_stable(self):
        self.assertEqual(make_image_key(b"same"), make_image_key(b"same"))

    def test_command_message_detection(self):
        def ev(text):
            class E:
                message_str = text

                def get_message_str(self):
                    return text
            return E()

        true_cases = ["/搜图", "/搜图 cat", "搜图", "。搜图 x", "/找图 y",
                      "/soutu z", "/搜图帮助", "!搜图"]
        for t in true_cases:
            self.assertTrue(_is_command_message(ev(t)), f"应识别为指令: {t!r}")
        false_cases = ["普通聊天", "帮我搜图", "/其它指令"]
        for t in false_cases:
            self.assertFalse(_is_command_message(ev(t)), f"不应识别为指令: {t!r}")

    def test_command_attached_forms_detected(self):
        """[工程师已修复 #5] 紧贴指令名的形式也必须被识别为指令（消除重复回复风险）。"""
        def ev(text):
            class E:
                def get_message_str(self):
                    return text
            return E()

        risky = ["/搜图cat", "/搜图http://x", "搜图帮助x", "/soutuhelp"]
        missed = [t for t in risky if not _is_command_message(ev(t))]
        # [工程师已修复 #5] 紧贴指令名的形式也必须识别为指令 → missed 应为空
        self.assertEqual(missed, [], f"应全部识别为指令，实际漏判: {missed}")


class TestCooldownBehavior(unittest.TestCase):
    """自动搜图冷却：同会话跳过、异会话独立。"""

    def _plugin_with_fake_search(self):
        p = SoutuSearchPlugin(object(), {"auto_search_cooldown": 60, "cache_ttl": 0})
        calls = {"n": 0}

        async def fake_from_event(event):
            return ImagePayload(data=b"img", mime="image/jpeg", filename="q.jpg")

        async def fake_search(*a, **k):
            calls["n"] += 1
            return SourceOutcome(results=[SearchResult("T", "S", "https://u", None, 90.0, {})])

        p.image_source.from_event = fake_from_event  # type: ignore
        p.soutu.search = fake_search  # type: ignore

        class Ev:
            def __init__(self, sess):
                self.unified_msg_origin = sess
                self.message_str = ""
                self.emitted = []

            def get_message_str(self):
                return self.message_str

            def plain_result(self, t):
                self.emitted.append(("plain", t))
                return ("plain", t)

            def chain_result(self, c):
                self.emitted.append(("chain", c))
                return ("chain", c)

        return p, calls, Ev

    def test_same_session_second_call_skipped(self):
        p, calls, Ev = self._plugin_with_fake_search()

        async def go():
            e = Ev("sess-1")
            r1 = [x async for x in p.on_message(e)]
            r2 = [x async for x in p.on_message(Ev("sess-1"))]
            return r1, r2

        r1, r2 = run(go())
        self.assertTrue(r1, "首次应回复")
        self.assertEqual(r2, [], "冷却期内第二次应静默跳过")
        self.assertEqual(calls["n"], 1, "冷却期内不应再次请求接口")

    def test_different_sessions_independent(self):
        p, calls, Ev = self._plugin_with_fake_search()

        async def go():
            [x async for x in p.on_message(Ev("sess-A"))]
            [x async for x in p.on_message(Ev("sess-B"))]

        run(go())
        self.assertEqual(calls["n"], 2, "不同会话互不影响，应各自请求")

    def test_command_message_not_auto_replied(self):
        p, calls, Ev = self._plugin_with_fake_search()

        async def go():
            e = Ev("s1")
            e.message_str = "/搜图"  # 指令消息
            return [x async for x in p.on_message(e)]

        out = run(go())
        self.assertEqual(out, [], "指令消息不应被 on_message 处理，避免重复回复")
        self.assertEqual(calls["n"], 0)


# ===========================================================================
# G. 对抗性：长度 / 资源
# ===========================================================================
class TestAdversarial(unittest.TestCase):
    def test_long_output_length(self):
        """[工程师已修复 #2] 超长输出必须按字符边界截断并追加后缀（默认上限 1200 字符）。"""
        results = [
            SearchResult(title="标" * 400, source="NH本子",
                         url="https://nhentai.net/g/123456/999/", score=99.9, extra={"page_no": 999})
            for _ in range(20)
        ]
        blocks = format_outcome(SourceOutcome(results=results), nsfw_send_image=False,
                                max_results=20, header="H")
        text = "\n".join(b.get("text", "") for b in blocks)
        # 修复后应被截断到 max_chars=1200 以内，并追加截断提示
        self.assertLessEqual(len(text), 1200, f"实际长度 {len(text)}，未受长度上限约束")
        self.assertIn("已截断", text, "缺少截断提示后缀")

    def test_output_with_warning_and_no_results(self):
        blocks = format_outcome(SourceOutcome(results=[], warnings=["站点限流"]),
                                nsfw_send_image=False, max_results=3, header="H")
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertIn("没有找到", joined)
        self.assertIn("站点限流", joined)

    def test_session_closed_after_close(self):
        from astrbot_plugin_soutu_search.core.soutu_client import SoutuClient

        async def go():
            c = SoutuClient(base_url="https://soutubot.moe", timeout=30)
            s = await c._get_session()
            self.assertFalse(s.closed)
            await c.close()
            self.assertTrue(s.closed)
            await c.close()  # 幂等

        run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
