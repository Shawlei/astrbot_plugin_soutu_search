"""P2 修复回归测试（工程师）。

覆盖 team-lead 回派的修复项：
- #1 soutubot 200+非 JSON → 统一包装 RuntimeError
- #2 回复正文长度截断
- #3 缓存移除死代码锁
- #5 指令判定（紧贴形式误报防护）
- #6 图片来源限制（协议白名单 / 内网拦截 / 目录白名单）
- #7 图片大小与魔数校验

复用 tests/test_core.py 的 astrbot 桩（import 即完成 sys.modules 装配）。
运行::
    python -m unittest tests.test_hardening -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search.core.cache import TTLCache  # noqa: E402
from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.image_source import (  # noqa: E402
    ImageSource,
    detect_image_mime,
)
from astrbot_plugin_soutu_search.core.soutu_client import SoutuClient  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    SoutuSearchPlugin,
    _command_head,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def run(coro):
    return asyncio.run(coro)


# ===========================================================================
# #1 soutubot 非 JSON 包装
# ===========================================================================
class FakeResp:
    def __init__(self, status=200, body="", raise_exc=None):
        self.status = status
        self._body = body
        self._raise = raise_exc

    async def text(self):
        return self._body

    async def read(self):
        return self._body.encode()

    async def __aenter__(self):
        if self._raise:
            raise self._raise
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.closed = False

    def post(self, url, **kw):
        return self._resp

    def get(self, url, **kw):
        return self._resp


class TestSoutuNonJson(unittest.TestCase):
    def _client(self, resp):
        c = SoutuClient(base_url="https://soutubot.moe", timeout=30, min_score=0)
        c._session = FakeSession(resp)
        return c

    def test_200_html_wrapped_as_runtimeerror(self):
        """Cloudflare 以 200 返回 HTML → 必须包装为可读 RuntimeError，而非裸 JSONDecodeError。"""
        c = self._client(FakeResp(200, "<html><title>Just a moment...</title></html>"))
        with self.assertRaises(RuntimeError) as ctx:
            run(c.search(b"img"))
        self.assertIn("非 JSON", str(ctx.exception))

    def test_200_valid_json_ok(self):
        body = json.dumps({"schema_version": "2.2", "status": "ok", "results": []})
        c = self._client(FakeResp(200, body))
        outcome = run(c.search(b"img"))
        self.assertEqual(outcome.results, [])

    def test_500_wrapped_with_status(self):
        c = self._client(FakeResp(500, "server error"))
        with self.assertRaises(RuntimeError) as ctx:
            run(c.search(b"img"))
        self.assertIn("500", str(ctx.exception))

    def test_timeout_wrapped(self):
        c = self._client(FakeResp(200, "", raise_exc=asyncio.TimeoutError()))
        with self.assertRaises(RuntimeError) as ctx:
            run(c.search(b"img"))
        self.assertIn("超时", str(ctx.exception))


# ===========================================================================
# #2 正文长度截断
# ===========================================================================
class TestReplyTruncation(unittest.TestCase):
    def _long_outcome(self, n=20):
        return SourceOutcome(
            results=[
                SearchResult(title="标" * 400, source="NH本子",
                             url="https://site/detail/very/long/path/123456", score=99.9, extra={})
                for _ in range(n)
            ]
        )

    def test_truncated_within_limit(self):
        blocks = format_outcome(self._long_outcome(), nsfw_send_image=False,
                                max_results=20, header="H", max_chars=1200)
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertLessEqual(len(text), 1200)
        self.assertIn("已截断", text)

    def test_custom_limit(self):
        blocks = format_outcome(self._long_outcome(), nsfw_send_image=False,
                                max_results=20, header="H", max_chars=300)
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertLessEqual(len(text), 300)
        self.assertIn("已截断", text)

    def test_zero_means_unlimited(self):
        blocks = format_outcome(self._long_outcome(), nsfw_send_image=False,
                                max_results=20, header="H", max_chars=0)
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertGreater(len(text), 4500)
        self.assertNotIn("已截断", text)

    def test_short_output_not_touched(self):
        outcome = SourceOutcome(results=[SearchResult("T", "S", "https://u", None, 9.0, {})])
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3,
                                header="H", max_chars=1200)
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertNotIn("已截断", text)
        self.assertIn("https://u", text)

    def test_image_blocks_preserved_when_truncated(self):
        outcome = SourceOutcome(
            results=[
                SearchResult(title="标" * 400, source="NH本子",
                             url="https://site/detail/very/long/path/123456",
                             thumbnail=f"https://thumb/{i}.jpg", score=99.9, extra={})
                for i in range(3)
            ]
        )
        blocks = format_outcome(outcome, nsfw_send_image=True,
                                max_results=3, header="H", max_chars=100)
        self.assertEqual(len([b for b in blocks if b["type"] == "image"]), 3)


# ===========================================================================
# #3 缓存死代码
# ===========================================================================
class TestCacheNoDeadLock(unittest.TestCase):
    def test_no_lock_attribute(self):
        self.assertFalse(hasattr(TTLCache(), "_lock"), "不应保留未使用的 _lock")

    def test_still_works(self):
        c = TTLCache(default_ttl=100)
        c.set("k", "v")
        self.assertEqual(c.get("k"), "v")
        self.assertIn("k", c)


# ===========================================================================
# #5 指令判定（紧贴形式误报防护）
# ===========================================================================
class TestCommandDetection(unittest.TestCase):
    def test_attached_forms_detected(self):
        for t in ["/搜图cat", "/搜图http://x", "搜图帮助x", "/soutuhelp", "/搜图", "。搜图 x"]:
            self.assertTrue(_command_head(t) is not None, f"应识别为指令: {t!r}")
        for t in ["普通聊天", "帮我搜图", "/其它指令"]:
            self.assertFalse(_command_head(t) is not None, f"不应识别为指令: {t!r}")


# ===========================================================================
# #6 图片来源限制
# ===========================================================================
class TestSourceSafety(unittest.TestCase):
    def test_non_http_scheme_rejected(self):
        for bad in ("ftp://evil.example/x.jpg", "gopher://evil.example/x", "ws://evil/x"):
            with self.assertRaises(ValueError):
                run(ImageSource(timeout=5).from_source(bad))

    def test_private_and_reserved_hosts_rejected(self):
        for bad in (
            "http://127.0.0.1/a.jpg",
            "http://10.0.0.5/a.jpg",
            "http://172.16.0.1/a.jpg",
            "http://192.168.1.10/a.jpg",
            "http://169.254.169.254/latest",
            "http://localhost/a.jpg",
            "http://[::1]/a.jpg",
        ):
            with self.assertRaises(RuntimeError):
                run(ImageSource(timeout=5).from_source(bad))

    def test_local_outside_root_rejected(self):
        tmp = Path(__file__).resolve().parent
        probe = tmp / "_safety_probe.png"
        probe.write_bytes(PNG)
        other_root = tmp / "_other_root"
        other_root.mkdir(exist_ok=True)
        try:
            with self.assertRaises(PermissionError):
                run(ImageSource(timeout=5, allowed_roots=[other_root]).from_source(str(probe)))
        finally:
            probe.unlink(missing_ok=True)
            shutil.rmtree(other_root, ignore_errors=True)

    def test_local_inside_root_allowed(self):
        root = Path(__file__).resolve().parent / "_safe_root"
        root.mkdir(exist_ok=True)
        probe = root / "ok.png"
        probe.write_bytes(PNG)
        try:
            payload = run(ImageSource(timeout=5, allowed_roots=[root]).from_source(str(probe)))
            self.assertEqual(payload.data, PNG)
            self.assertEqual(payload.source_kind, "file")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_default_rejects_all_local(self):
        probe = Path(__file__).resolve().parent / "_no_root.png"
        probe.write_bytes(PNG)
        try:
            with self.assertRaises(PermissionError):
                run(ImageSource(timeout=5).from_source(str(probe)))
        finally:
            probe.unlink(missing_ok=True)


# ===========================================================================
# #7 大小与魔数校验
# ===========================================================================
class TestImageValidation(unittest.TestCase):
    def test_detect_mime(self):
        self.assertEqual(detect_image_mime(PNG), "image/png")
        self.assertEqual(detect_image_mime(JPEG), "image/jpeg")
        self.assertEqual(detect_image_mime(b"GIF89a.."), "image/gif")
        self.assertEqual(detect_image_mime(b"RIFF\x00\x00\x00\x00WEBP"), "image/webp")
        self.assertEqual(detect_image_mime(b"BM....."), "image/bmp")
        self.assertIsNone(detect_image_mime(b"not-an-image"))
        self.assertIsNone(detect_image_mime(b""))

    def test_oversize_rejected(self):
        big = PNG + b"\x00" * 100
        with self.assertRaises(RuntimeError) as ctx:
            run(ImageSource(timeout=5, max_image_bytes=64).from_source(
                "data:image/png;base64," + base64.b64encode(big).decode()))
        self.assertIn("过大", str(ctx.exception))

    def test_non_image_magic_rejected(self):
        uri = "data:image/png;base64," + base64.b64encode(b"hello world").decode()
        with self.assertRaises(RuntimeError):
            run(ImageSource(timeout=5).from_source(uri))

    def test_mime_derived_from_magic_not_declared(self):
        # 声明为 jpeg，实际是 png → mime 应以魔数为准
        uri = "data:image/jpeg;base64," + base64.b64encode(PNG).decode()
        payload = run(ImageSource(timeout=5).from_source(uri))
        self.assertEqual(payload.mime, "image/png")

    def test_valid_png_data_uri_ok(self):
        uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
        payload = run(ImageSource(timeout=5).from_source(uri))
        self.assertEqual(payload.data, PNG)
        self.assertEqual(payload.source_kind, "data")


# ===========================================================================
# 第二轮 P1-1：SSRF 域名/混淆 IP 绕过
# ===========================================================================
class TestSsrfHostBlocking(unittest.TestCase):
    def test_obfuscated_and_wildcard_hosts_blocked(self):
        """QA 实测的绕过向量必须全部被 _host_blocked 拦截。"""
        for host in (
            "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.10",
            "169.254.169.254", "localhost", "::1", "[::1]", "0.0.0.0",
            "2130706433",       # 十进制整数
            "0x7f000001",       # 十六进制
            "0177.0.0.1",       # 八进制
            "127.1",            # inet_aton 短写
            "::ffff:127.0.0.1", # IPv4-mapped IPv6
            "127.0.0.1.nip.io", # 通配 DNS（解析失败亦 fail-closed 拦截）
        ):
            self.assertTrue(ImageSource._host_blocked(host), f"应拦截: {host!r}")

    def test_public_literal_ip_allowed(self):
        # 公网字面量 IP 不应被误拦（且无需 DNS）
        self.assertFalse(ImageSource._host_blocked("1.2.3.4"))
        self.assertFalse(ImageSource._host_blocked("8.8.8.8"))

    def test_empty_host_blocked(self):
        for host in ("", None, "   ", "[]"):
            self.assertTrue(ImageSource._host_blocked(host))

    def test_fail_closed_on_unresolvable_domain(self):
        # 解析失败（不可达/不存在域名）必须视为拒绝
        self.assertTrue(ImageSource._host_blocked("this-domain-should-not-exist.invalid"))

    def test_from_source_rejects_obfuscated_url(self):
        for url in ("http://2130706433/a.jpg", "http://0x7f000001/a.jpg",
                    "http://127.1/a.jpg", "http://127.0.0.1.nip.io/a.jpg"):
            with self.assertRaises(RuntimeError):
                run(ImageSource(timeout=5).from_source(url))

    def test_host_is_blocked_async_wrapper(self):
        src = ImageSource(timeout=5)

        async def go():
            try:
                return await src._host_is_blocked("127.0.0.1")
            finally:
                await src.close()

        self.assertTrue(run(go()))


class _HdrResp:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _SeqSession:
    """按调用顺序返回预设响应，用于模拟重定向链。"""

    def __init__(self, resps):
        self._resps = list(resps)
        self.closed = False

    def get(self, url, **kw):
        return self._resps.pop(0)

    async def close(self):
        self.closed = True


class TestRedirectSafety(unittest.TestCase):
    def _src_with_redirect(self, location):
        src = ImageSource(timeout=5)
        src._session = _SeqSession([_HdrResp(302, {"Location": location})])
        return src

    def test_redirect_to_private_blocked(self):
        src = self._src_with_redirect("http://127.0.0.1/secret.jpg")

        async def go():
            try:
                await src.fetch_url("http://1.2.3.4/a.jpg")  # 原始 host 为公网字面量
            finally:
                await src.close()

        with self.assertRaises(RuntimeError):
            run(go())

    def test_redirect_to_disallowed_scheme_blocked(self):
        src = self._src_with_redirect("file:///etc/passwd")

        async def go():
            try:
                await src.fetch_url("http://1.2.3.4/a.jpg")
            finally:
                await src.close()

        with self.assertRaises(RuntimeError):
            run(go())

    def test_redirect_without_location_blocked(self):
        src = ImageSource(timeout=5)
        src._session = _SeqSession([_HdrResp(302, {})])

        async def go():
            try:
                await src.fetch_url("http://1.2.3.4/a.jpg")
            finally:
                await src.close()

        with self.assertRaises(RuntimeError):
            run(go())

    def test_redirect_chain_too_long(self):
        # 一直 302 到自身 → 超过上限应中止
        src = ImageSource(timeout=5)
        src._session = _SeqSession([_HdrResp(302, {"Location": "http://1.2.3.4/a.jpg"}) for _ in range(20)])

        async def go():
            try:
                await src.fetch_url("http://1.2.3.4/a.jpg")
            finally:
                await src.close()

        with self.assertRaises(RuntimeError):
            run(go())


# ===========================================================================
# 第二轮 P2-1：指令判定精确性（紧贴识别 + 误报消除）
# ===========================================================================
class TestCommandPrecision(unittest.TestCase):
    def test_should_be_command(self):
        for t in ("/搜图", "/搜图cat", "/搜图http://x", "搜图帮助x", "/soutuhelp",
                  "。搜图 x", "!找图", "搜图 cat",
                  "/搜本", "/搜本子", "/搜本 猫娘", "/搜本 http://a.com/x.jpg",
                  "/搜本帮助", "/soutu 猫娘"):
            self.assertTrue(_command_head(t) is not None, f"应识别为指令: {t!r}")

    def test_should_not_be_command(self):
        for t in ("搜图真有意思", "soutubot很棒", "找图…", "搜图帮助…",
                  "普通聊天", "帮我搜图", "搜索图片", "/其它指令",
                  "搜本真好看", "搜本子真好看"):
            self.assertFalse(_command_head(t) is not None, f"不应识别为指令: {t!r}")

    def test_cjk_keyword_args_are_commands(self):
        """回归：带中文关键词参数（空白分隔）的指令必须被识别。"""
        for t in ("/搜图 初音未来", "/搜图 甘雨", "/找图 蔚蓝档案", "搜图 初音未来",
                  "/搜图  双空格中文", "/搜图 中文关键词 https://x.com/a.jpg",
                  "/搜图 http://a.com/x.jpg",
                  "/搜本 猫娘", "/搜本 猫娘 白丝", "/soutu 猫娘"):
            self.assertTrue(_command_head(t) is not None, f"应识别为指令: {t!r}")

    def test_acceptance_table(self):
        """team-lead 验收表逐条核对（0.4.0 起含「搜本」拆分支）。"""
        expect_true = [
            "/搜图", "/搜图cat", "/soutuhelp", "搜图帮助x", "。搜图 x", "!找图",
            "/搜图 http://a.com/x.jpg",
            "/搜图 初音未来", "/搜图 甘雨", "/找图 蔚蓝档案", "搜图 初音未来", "/搜图  双空格中文",
            "/搜图 中文关键词 https://x.com/a.jpg",
            "/搜本", "/搜本子", "/搜本 猫娘", "/搜本 http://a.com/x.jpg",
            "/搜本帮助", "/soutu 猫娘",
        ]
        expect_false = [
            "搜图真有意思", "soutubot很棒", "普通聊天", "帮我搜图", "搜索图片", "/其它指令",
            "搜图帮助…", "找图…", "搜本真好看", "搜本子真好看",
        ]
        for t in expect_true:
            self.assertTrue(_command_head(t) is not None, f"[验收] 应为 True: {t!r}")
        for t in expect_false:
            self.assertFalse(_command_head(t) is not None, f"[验收] 应为 False: {t!r}")

    def test_cjk_keyword_arg_recovered(self):
        """参数还原也应拿到中文关键词。"""
        from astrbot_plugin_soutu_search.main import _recover_command_args

        class E:
            def get_message_str(self):
                return "/搜图 初音未来"

        self.assertEqual(_recover_command_args(E()), "初音未来")


# ===========================================================================
# 第二轮 P2-2：截断极值不得越界
# ===========================================================================
class TestTruncationExtremes(unittest.TestCase):
    def _long_blocks(self, mc):
        results = [
            SearchResult(title="标" * 400, source="NH本子",
                         url="https://nhentai.net/g/123456/999/", score=99.9, extra={})
            for _ in range(20)
        ]
        return format_outcome(SourceOutcome(results=results), nsfw_send_image=False,
                              max_results=20, header="H", max_chars=mc)

    def test_all_limits_within_bound(self):
        for mc in (1, 5, 10, 11, 12, 15, 20, 50, 100, 300, 1200):
            text = "\n".join(b.get("text", "") for b in self._long_blocks(mc))
            self.assertLessEqual(len(text), mc, f"max_chars={mc} 越界: {len(text)}")

    def test_zero_and_negative_unlimited(self):
        for mc in (0, -1):
            blocks = format_outcome(
                SourceOutcome(results=[SearchResult("T" * 400, "S", "https://u", None, 9.0, {})]),
                nsfw_send_image=False, max_results=3, header="H", max_chars=mc,
            )
            text = "\n".join(b.get("text", "") for b in blocks)
            self.assertNotIn("已截断", text)

    def test_suffix_present_when_room(self):
        text = "\n".join(b.get("text", "") for b in self._long_blocks(1200))
        self.assertIn("已截断", text)

    def test_no_image_leak_under_truncation(self):
        results = [
            SearchResult(title="标" * 400, source="S", url="https://d/x",
                         thumbnail="https://cdn.secret/leak.jpg", score=99.0, extra={})
            for _ in range(5)
        ]
        blocks = format_outcome(SourceOutcome(results=results), nsfw_send_image=False,
                                max_results=5, header="H", max_chars=100)
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertFalse(any(b["type"] == "image" for b in blocks))
        self.assertNotIn("leak.jpg", joined)
        self.assertLessEqual(len(joined), 100)


# ===========================================================================
# 第三轮 P2-3：测试不得在工作区制造 MagicMock/ 垃圾目录
# ===========================================================================
class TestNoMagicMockPollution(unittest.TestCase):
    def test_data_dir_is_real_not_plugin_root(self):
        """插件的 data_dir 必须落到真实的（临时）目录，而非插件根/MagicMock。"""
        p = SoutuSearchPlugin(object(), {})
        data_dir = Path(p.data_dir).resolve()
        plugin_root = PLUGIN_ROOT.resolve()
        self.assertNotEqual(data_dir, plugin_root)
        self.assertNotIn(plugin_root, data_dir.parents, f"data_dir 不应位于插件根之下: {data_dir}")
        self.assertNotEqual(data_dir.name, "MagicMock")

    def test_repeated_instantiation_creates_no_garbage_dir(self):
        """多次构造插件不得在插件根目录生成 MagicMock/（历史上会，现应修复）。"""
        for _ in range(5):
            SoutuSearchPlugin(object(), {})
        self.assertFalse((PLUGIN_ROOT / "MagicMock").exists(),
                         "插件根目录不应出现 MagicMock/ 垃圾目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
