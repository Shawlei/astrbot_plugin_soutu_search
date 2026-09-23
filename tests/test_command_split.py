"""指令拆分（0.4.0 破坏性变更）测试：三条互斥指令 + 帮助指令。

映射（自 0.4.0 起）：

| 指令 | 别名 | 职责 |
|---|---|---|
| ``搜本`` | ``搜本子`` / ``soutu`` / ``找图`` | soutubot 以图搜本子（**只接受图片**） |
| ``搜本帮助`` | ``搜本help`` / ``soutuhelp`` | |
| ``搜图`` | 无 | Safebooru 关键词搜图（**只接受关键词**） |
| ``搜图帮助`` | ``搜图help`` | |
| ``搜P站`` | ``pixiv`` / ``saucenao`` | SauceNAO 反查（行为不变） |
| ``搜P站帮助`` | ``搜P站help`` / ``saucenaohelp`` | |

覆盖：
- ``_COMMAND_NAMES`` 顺序（帮助类在前、「搜本子」在「搜本」前）；
- 各指令 / 别名 / 人话连读的判定语义（含 ``搜本`` 新增用例）；
- ``搜本``：图片 / 图片直链走 soutubot；纯关键词回引导且**不搜索**；无参回帮助；
- ``搜图``：图片 / 图片直链回引导且**不下载不搜索**；关键词走 Safebooru；无参回帮助；
- 引导与帮助文案按实际命令前缀渲染；
- 访问控制覆盖 ``搜本`` 与 ``搜本帮助``。

运行::
    python -m unittest tests.test_command_split -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot.api.message_components import Image  # noqa: E402  (mocked)

from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
)
from astrbot_plugin_soutu_search.core.image_source import ImagePayload  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    ACCESS_DENIED_TEXT,
    BOOK_HELP_TEXT,
    BOOK_KEYWORD_NOT_SUPPORTED_TEXT,
    HELP_TEXT,
    IMAGE_NOT_SUPPORTED_TEXT,
    SAUCENAO_HELP_TEXT,
    SoutuSearchPlugin,
    _COMMAND_NAMES,
    _command_head,
    _recover_command_args,
    _render_book_help,
    _render_book_keyword_hint,
    _render_help,
    _render_image_not_supported_hint,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [x async for x in agen]


def collect(agen):
    return run(_collect(agen))


class FakeEvent:
    """可携带图片组件的消息事件替身。"""

    def __init__(self, umo="umo-A", text="", with_image=False):
        self.unified_msg_origin = umo
        self.message_str = text
        message = [Image(url="http://img.example/q.jpg")] if with_image else []
        self.message_obj = type("M", (), {"message_id": "m1", "message": message})()

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, comps):
        return ("chain", comps)


class _StubSoutu:
    def __init__(self):
        self.calls = []

    async def search(self, image, **kw):
        self.calls.append(kw)
        return SourceOutcome(
            results=[SearchResult(title="t", source="s", url="u", score=90.0)]
        )

    async def close(self):
        pass


class _StubBooru:
    def __init__(self):
        self.calls = []

    async def search_by_tags(self, tags, **kw):
        self.calls.append(tags)
        return SourceOutcome(
            results=[SearchResult(title="t", source="s", url="u", score=90.0)]
        )

    async def close(self):
        pass


def make_plugin(**cfg):
    return SoutuSearchPlugin(object(), cfg)


# ===========================================================================
# 1. _COMMAND_NAMES 顺序与内容
# ===========================================================================
class TestCommandNames(unittest.TestCase):
    def test_exact_order(self):
        """必须严格等于规格给定顺序（帮助类在前、「搜本子」在「搜本」前）。"""
        self.assertEqual(
            _COMMAND_NAMES,
            (
                "搜本帮助", "搜本help", "soutuhelp",
                "搜图帮助", "搜图help",
                "搜P站帮助", "搜P站help", "saucenaohelp",
                "搜本子",
                "搜P站", "saucenao", "pixiv",
                "搜本", "soutu", "找图",
                "搜图",
            ),
        )

    def test_help_names_before_their_bodies(self):
        idx = {name: i for i, name in enumerate(_COMMAND_NAMES)}
        for help_name, body in (
            ("搜本帮助", "搜本"),
            ("搜图帮助", "搜图"),
            ("搜P站帮助", "搜P站"),
        ):
            self.assertLess(idx[help_name], idx[body], f"{help_name} 必须排在 {body} 之前")

    def test_book_child_before_book(self):
        idx = {name: i for i, name in enumerate(_COMMAND_NAMES)}
        self.assertLess(idx["搜本子"], idx["搜本"], "「搜本子」必须排在「搜本」之前")

    def test_no_duplicates(self):
        self.assertEqual(len(_COMMAND_NAMES), len(set(_COMMAND_NAMES)))


# ===========================================================================
# 2. 指令判定 / 别名 / 人话连读
# ===========================================================================
class TestCommandHeadSplit(unittest.TestCase):
    def test_book_domain_commands(self):
        for text, want in (
            ("/搜本", "搜本"),
            ("搜本", "搜本"),
            ("/搜本 猫娘", "搜本"),
            ("/搜本子", "搜本子"),
            ("搜本子", "搜本子"),
            ("/soutu", "soutu"),
            ("/找图", "找图"),
            ("!找图", "找图"),
            ("/soutu z", "soutu"),
            ("/搜本 http://a.com/x.jpg", "搜本"),
            ("#搜本 猫娘", "搜本"),
        ):
            self.assertEqual(_command_head(text), want, f"应识别为指令: {text!r}")

    def test_book_help_commands(self):
        for text, want in (
            ("/搜本帮助", "搜本帮助"),
            ("搜本帮助", "搜本帮助"),
            ("搜本帮助x", "搜本帮助"),
            ("/搜本help", "搜本help"),
            ("/soutuhelp", "soutuhelp"),
            ("#soutuhelp", "soutuhelp"),
        ):
            self.assertEqual(_command_head(text), want, f"应识别为指令: {text!r}")

    def test_book_human_continuation_not_command(self):
        """新增指令的「人话连读」必须不被误判为指令。"""
        for text in ("搜本真好看", "搜本子真好看", "soutu本子真棒", "/搜本真好看",
                     "搜本子不错", "找图…"):
            self.assertIsNone(_command_head(text), f"不应识别为指令: {text!r}")

    def test_search_human_continuation_not_command(self):
        for text in ("搜图真有意思", "soutubot很棒", "找图…", "搜图帮助…", "帮我搜图"):
            self.assertIsNone(_command_head(text), f"不应识别为指令: {text!r}")

    def test_search_and_pixiv_still_commands(self):
        for text, want in (
            ("/搜图", "搜图"),
            ("搜图", "搜图"),
            ("/搜图 cat_ears", "搜图"),
            ("。搜图 x", "搜图"),
            ("/搜图帮助", "搜图帮助"),
            ("/搜图help", "搜图help"),
            ("/搜P站", "搜P站"),
            ("/pixiv", "pixiv"),
            ("/saucenao", "saucenao"),
            ("/搜P站帮助", "搜P站帮助"),
        ):
            self.assertEqual(_command_head(text), want, f"应识别为指令: {text!r}")

    def test_recover_args_book(self):
        self.assertEqual(_recover_command_args(FakeEvent(text="/搜本 猫娘 白丝")), "猫娘 白丝")
        self.assertEqual(_recover_command_args(FakeEvent(text="/soutu 猫娘")), "猫娘")
        self.assertEqual(_recover_command_args(FakeEvent(text="/搜本子")), "")
        self.assertEqual(
            _recover_command_args(FakeEvent(text="/搜本 http://a.com/x.jpg")),
            "http://a.com/x.jpg",
        )

    def test_recover_args_search(self):
        self.assertEqual(_recover_command_args(FakeEvent(text="/搜图 cat_ears")), "cat_ears")
        self.assertEqual(_recover_command_args(FakeEvent(text="/搜图 甘雨")), "甘雨")


# ===========================================================================
# 3. 「搜本」行为
# ===========================================================================
class TestBookCommand(unittest.TestCase):
    def _p_with_event_image(self):
        p = make_plugin()
        stub = _StubSoutu()
        p.soutu = stub  # type: ignore[assignment]

        async def fake_from_event(event):
            return ImagePayload(data=PNG, mime="image/png", filename="q.png")

        p.image_source.from_event = fake_from_event  # type: ignore[assignment]
        return p, stub

    def test_book_with_message_image_searches_soutubot(self):
        p, stub = self._p_with_event_image()
        out = collect(p.book_cmd(FakeEvent(text="/搜本"), ""))
        self.assertEqual(len(stub.calls), 1, "「搜本」+ 图片应走 soutubot")
        self.assertEqual(out[0][0], "chain")

    def test_book_with_image_url_downloads_then_searches(self):
        p = make_plugin()
        stub = _StubSoutu()
        p.soutu = stub  # type: ignore[assignment]
        calls = []

        async def fake_from_source(src):
            calls.append(src)
            return ImagePayload(data=PNG, mime="image/png", filename="a.png")

        p.image_source.from_source = fake_from_source  # type: ignore[assignment]
        out = collect(p.book_cmd(FakeEvent(text="/搜本 http://x/a.jpg"), "http://x/a.jpg"))
        self.assertEqual(calls, ["http://x/a.jpg"])
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(out[0][0], "chain")

    def test_book_keyword_hinted_and_not_searched(self):
        p = make_plugin()
        stub = _StubSoutu()
        stub_booru = _StubBooru()
        p.soutu = stub  # type: ignore[assignment]
        p.booru = stub_booru  # type: ignore[assignment]
        out = collect(p.book_cmd(FakeEvent(text="/搜本 猫娘"), "猫娘"))
        self.assertEqual(stub.calls, [], "「搜本」收到关键词不得以图搜图")
        self.assertEqual(stub_booru.calls, [], "「搜本」收到关键词不得走 Safebooru")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "plain")
        self.assertEqual(out[0][1], BOOK_KEYWORD_NOT_SUPPORTED_TEXT.format(p="/"))
        self.assertIn("只支持图片", out[0][1])

    def test_book_no_args_returns_book_help(self):
        p = make_plugin()
        out = collect(p.book_cmd(FakeEvent(text="/搜本"), ""))
        self.assertEqual(out[0][1], BOOK_HELP_TEXT)

    def test_book_help_subword(self):
        for trigger in ("帮助", "help", "-h", "--help", "用法"):
            p = make_plugin()
            out = collect(p.book_cmd(FakeEvent(text=f"/搜本 {trigger}"), trigger))
            self.assertEqual(out[0][1], BOOK_HELP_TEXT, f"「搜本 {trigger}」应回搜本帮助")

    def test_book_help_cmd(self):
        p = make_plugin()
        out = collect(p.book_help_cmd(FakeEvent(text="/搜本帮助")))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "plain")
        self.assertEqual(out[0][1], BOOK_HELP_TEXT)


# ===========================================================================
# 4. 「搜图」行为
# ===========================================================================
class TestSearchCommand(unittest.TestCase):
    def test_search_with_message_image_hinted_no_download_no_search(self):
        p = make_plugin()
        stub_soutu = _StubSoutu()
        stub_booru = _StubBooru()
        p.soutu = stub_soutu  # type: ignore[assignment]
        p.booru = stub_booru  # type: ignore[assignment]
        calls = []

        async def fake_from_source(src):
            calls.append(src)
            return ImagePayload(data=PNG, mime="image/png", filename="a.png")

        p.image_source.from_source = fake_from_source  # type: ignore[assignment]

        async def boom_from_event(event):
            raise AssertionError("「搜图」不得调用 from_event / 下载图片")

        p.image_source.from_event = boom_from_event  # type: ignore[assignment]

        out = collect(p.sou_cmd(FakeEvent(text="/搜图", with_image=True), ""))
        self.assertEqual(calls, [], "「搜图」不得下载图片")
        self.assertEqual(stub_soutu.calls, [], "「搜图」不得走以图搜图")
        self.assertEqual(stub_booru.calls, [], "「搜图」不得把图片当关键词搜")
        self.assertEqual(out[0][1], IMAGE_NOT_SUPPORTED_TEXT.format(p="/"))

    def test_search_with_image_url_hinted_no_download(self):
        p = make_plugin()
        stub_soutu = _StubSoutu()
        stub_booru = _StubBooru()
        p.soutu = stub_soutu  # type: ignore[assignment]
        p.booru = stub_booru  # type: ignore[assignment]
        calls = []

        async def fake_from_source(src):
            calls.append(src)
            return ImagePayload(data=PNG, mime="image/png", filename="a.png")

        p.image_source.from_source = fake_from_source  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 http://x/a.jpg"), "http://x/a.jpg"))
        self.assertEqual(calls, [], "「搜图」不得下载图片直链")
        self.assertEqual(stub_soutu.calls, [])
        self.assertEqual(stub_booru.calls, [])
        self.assertEqual(out[0][1], IMAGE_NOT_SUPPORTED_TEXT.format(p="/"))

    def test_search_keyword_goes_safebooru(self):
        p = make_plugin()
        stub = _StubBooru()
        p.booru = stub  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 cat_ears"), "cat_ears"))
        self.assertEqual(stub.calls, ["cat_ears"])
        self.assertEqual(out[0][0], "chain")

    def test_search_no_args_returns_help(self):
        p = make_plugin()
        out = collect(p.sou_cmd(FakeEvent(text="/搜图"), ""))
        self.assertEqual(out[0][1], HELP_TEXT)

    def test_search_help_subword(self):
        p = make_plugin()
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 帮助"), "帮助"))
        self.assertEqual(out[0][1], HELP_TEXT)

    def test_search_help_cmd(self):
        p = make_plugin()
        out = collect(p.sou_help_cmd(FakeEvent(text="/搜图帮助")))
        self.assertEqual(out[0][1], HELP_TEXT)


# ===========================================================================
# 5. 文案渲染（按实际前缀）
# ===========================================================================
class TestRendering(unittest.TestCase):
    def test_book_help_render(self):
        text = _render_book_help("#")
        self.assertIn("#搜本", text)
        self.assertIn("#搜本帮助", text)
        self.assertIn("#搜图", text)
        self.assertNotIn("/搜本", text)

    def test_book_help_default(self):
        self.assertIn("/搜本", BOOK_HELP_TEXT)
        self.assertIn("/搜本帮助", BOOK_HELP_TEXT)

    def test_search_help_render(self):
        text = _render_help("#")
        self.assertIn("#搜图 <关键词>", text)
        self.assertIn("#搜图帮助", text)
        self.assertNotIn("/搜图", text)

    def test_search_help_default(self):
        self.assertIn("/搜图 <关键词>", HELP_TEXT)
        self.assertIn("/搜图帮助", HELP_TEXT)

    def test_book_keyword_hint_render(self):
        text = _render_book_keyword_hint("#")
        self.assertIn("#搜图 <关键词>", text)
        self.assertIn("#搜P站", text)
        self.assertNotIn("/搜图", text)
        self.assertEqual(_render_book_keyword_hint(""), BOOK_KEYWORD_NOT_SUPPORTED_TEXT.format(p="/"))
        self.assertEqual(_render_book_keyword_hint(None), BOOK_KEYWORD_NOT_SUPPORTED_TEXT.format(p="/"))

    def test_image_not_supported_hint_render(self):
        text = _render_image_not_supported_hint("#")
        self.assertIn("#搜本", text)
        self.assertIn("#搜P站", text)
        self.assertNotIn("/搜本", text)
        self.assertEqual(
            _render_image_not_supported_hint(""), IMAGE_NOT_SUPPORTED_TEXT.format(p="/")
        )

    def test_plugin_hints_use_actual_prefix(self):
        ctx = type("C", (), {"get_config": lambda self: {"wake_prefix": ["#"]}})()
        p = SoutuSearchPlugin(ctx, {})
        self.assertIn("#搜本", p._book_help_text())
        self.assertIn("#搜图", p._book_keyword_hint())
        self.assertIn("#搜本", p._image_not_supported_hint())

    def test_saucenao_help_cross_reference(self):
        p = make_plugin()
        text = p._saucenao_help_text()
        self.assertIn("搜本", text)
        self.assertIn("搜图", text)
        self.assertIn("/搜P站", text)
        self.assertIn("/搜P站", SAUCENAO_HELP_TEXT)


# ===========================================================================
# 6. 访问控制覆盖「搜本」与「搜本帮助」
# ===========================================================================
class TestBookAccessControl(unittest.TestCase):
    def _denied_plugin(self):
        return make_plugin(access_mode="blacklist", blacklist=["umo-A"])

    def test_book_cmd_denied(self):
        p = self._denied_plugin()
        out = collect(p.book_cmd(FakeEvent(umo="umo-A", text="/搜本"), ""))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)

    def test_book_help_cmd_denied(self):
        p = self._denied_plugin()
        out = collect(p.book_help_cmd(FakeEvent(umo="umo-A", text="/搜本帮助")))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)

    def test_book_cmd_denied_zero_requests(self):
        p = make_plugin(access_mode="whitelist", whitelist=[])
        stub_soutu = _StubSoutu()
        p.soutu = stub_soutu  # type: ignore[assignment]
        fetched = []

        async def fake_from_event(event):
            fetched.append(1)
            return ImagePayload(data=PNG, mime="image/png", filename="q.png")

        p.image_source.from_event = fake_from_event  # type: ignore[assignment]
        out = collect(p.book_cmd(FakeEvent(umo="umo-A", text="/搜本"), ""))
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)
        self.assertEqual(fetched, [], "受限时应零取图")
        self.assertEqual(stub_soutu.calls, [], "受限时应零搜索")


if __name__ == "__main__":
    unittest.main(verbosity=2)
