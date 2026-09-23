"""指令映射（0.5.0）测试：两条指令 + 帮助指令 + 「搜图」自动判别。

映射（自 0.5.0 起）：

| 指令 | 别名 | 职责 |
|---|---|---|
| ``搜本`` | ``搜本子`` / ``soutu`` / ``找图`` | soutubot 以图搜本子（**只接受图片**） |
| ``搜本帮助`` | ``搜本help`` / ``soutuhelp`` | |
| ``搜图`` | ``pixiv`` / ``saucenao`` | **自动判别**：图片/图片链接 → SauceNAO 反查 Pixiv；纯关键词 → Safebooru |
| ``搜图帮助`` | ``搜图help`` / ``pixivhelp`` / ``saucenaohelp`` | |

覆盖：
- ``_COMMAND_NAMES`` 顺序（帮助类在前、「搜本子」在「搜本」前）；
- 各指令 / 别名 / 人话连读的判定语义；
- ``搜本``：图片 / 图片直链走 soutubot；纯关键词回引导且**不搜索**；无参回帮助；
- ``搜图`` 自动判别：图片 → SauceNAO；图片链接 → 下载后 SauceNAO；关键词 → Safebooru；
  无 api_key 时关键词仍可用、图片分支回 key 引导且**不下载**；无参回帮助；
- 引导与帮助文案按实际命令前缀渲染；
- 访问控制覆盖四条指令（``搜本`` / ``搜本帮助`` / ``搜图`` / ``搜图帮助``）。

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
    SAUCENAO_KEY_MISSING_TEXT,
    SoutuSearchPlugin,
    _COMMAND_NAMES,
    _command_head,
    _recover_command_args,
    _render_book_help,
    _render_book_keyword_hint,
    _render_help,
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


class _StubSaucenao:
    def __init__(self):
        self.calls = []

    async def search(self, image, **kw):
        self.calls.append(kw)
        return SourceOutcome(
            results=[SearchResult(title="t", source="s", url="u", score=90.0)]
        )

    async def close(self):
        pass


class _StubAscii2d:
    """ascii2d provider 替身（避免测试触发真实联网）。"""

    def __init__(self):
        self.calls = []

    async def search(self, image, **kw):
        self.calls.append(kw)
        return SourceOutcome(
            results=[SearchResult(title="a2d", source="Pixiv", url="https://p/1", score=None)]
        )

    async def close(self):
        pass


def _chain_text(result) -> str:
    """从 ``("plain", text)`` / ``("chain", comps)`` 结果中提取纯文本。"""
    kind, payload = result
    if kind == "plain":
        return payload
    return "\n".join(getattr(c, "text", "") for c in payload)


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
                "搜图帮助", "搜图help", "pixivhelp", "saucenaohelp",
                "搜本子",
                "搜本", "soutu", "找图",
                "搜图", "saucenao", "pixiv",
            ),
        )

    def test_help_names_before_their_bodies(self):
        idx = {name: i for i, name in enumerate(_COMMAND_NAMES)}
        for help_name, body in (
            ("搜本帮助", "搜本"),
            ("搜图帮助", "搜图"),
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
        for text in ("搜图真有意思", "soutubot很棒", "找图…", "搜图帮助…", "帮我搜图",
                     "pixiv很好用", "saucenao不错"):
            self.assertIsNone(_command_head(text), f"不应识别为指令: {text!r}")

    def test_search_and_alias_still_commands(self):
        for text, want in (
            ("/搜图", "搜图"),
            ("搜图", "搜图"),
            ("/搜图 cat_ears", "搜图"),
            ("。搜图 x", "搜图"),
            ("/搜图 http://a.com/x.jpg", "搜图"),
            ("/搜图帮助", "搜图帮助"),
            ("/搜图help", "搜图help"),
            ("/pixiv", "pixiv"),
            ("/saucenao", "saucenao"),
            ("#pixiv", "pixiv"),
            ("/pixivhelp", "pixivhelp"),
            ("/saucenaohelp", "saucenaohelp"),
            ("/pixiv 猫娘", "pixiv"),
        ):
            self.assertEqual(_command_head(text), want, f"应识别为指令: {text!r}")

    def test_removed_soutu_pixiv_cmd_is_not_command(self):
        """0.5.0 起 ``搜P站`` / ``搜P站帮助`` 已移除，不再是本插件指令。"""
        for text in ("/搜P站", "搜P站", "#搜P站", "/搜P站帮助", "搜P站帮助x", "/搜P站 猫娘"):
            self.assertIsNone(_command_head(text), f"不应识别为指令: {text!r}")

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
# 4. 「搜图」行为（自动判别）
# ===========================================================================
class TestSearchCommand(unittest.TestCase):
    def _plugin_with_saucenao(self, **cfg):
        p = make_plugin(**cfg)
        stub = _StubSaucenao()
        p.saucenao = stub  # type: ignore[assignment]
        return p, stub

    def test_search_with_message_image_goes_saucenao(self):
        """「搜图」+ 图片 → **双源并行**：SauceNAO 与 ascii2d 各 1 次，soutu 0 次。 [工程师已改 #9]"""
        p, stub_sa = self._plugin_with_saucenao(saucenao_api_key="k")
        stub_a2d = _StubAscii2d()
        stub_soutu = _StubSoutu()
        stub_booru = _StubBooru()
        p.ascii2d = stub_a2d  # type: ignore[assignment]
        p.soutu = stub_soutu  # type: ignore[assignment]
        p.booru = stub_booru  # type: ignore[assignment]

        async def fake_from_event(event):
            return ImagePayload(data=PNG, mime="image/png", filename="q.png")

        p.image_source.from_event = fake_from_event  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图", with_image=True), ""))
        self.assertEqual(len(stub_sa.calls), 1, "「搜图」+ 图片应走 SauceNAO")
        self.assertEqual(len(stub_a2d.calls), 1, "「搜图」+ 图片也应并行走 ascii2d")
        self.assertEqual(stub_soutu.calls, [], "「搜图」不得走 soutubot 以图搜图")
        self.assertEqual(stub_booru.calls, [], "「搜图」不得把图片当关键词")
        self.assertEqual(out[0][0], "chain")

    def test_search_with_image_url_downloads_then_saucenao(self):
        """「搜图」+ 图片直链 → 下载后**双源并行**（SauceNAO + ascii2d）。 [工程师已改 #10]"""
        p, stub_sa = self._plugin_with_saucenao(saucenao_api_key="k")
        stub_a2d = _StubAscii2d()
        stub_booru = _StubBooru()
        p.ascii2d = stub_a2d  # type: ignore[assignment]
        p.booru = stub_booru  # type: ignore[assignment]
        calls = []

        async def fake_from_source(src):
            calls.append(src)
            return ImagePayload(data=PNG, mime="image/png", filename="a.png")

        p.image_source.from_source = fake_from_source  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 http://x/a.jpg"), "http://x/a.jpg"))
        self.assertEqual(calls, ["http://x/a.jpg"], "应通过 from_source 下载图片链接")
        self.assertEqual(len(stub_sa.calls), 1)
        self.assertEqual(len(stub_a2d.calls), 1)
        self.assertEqual(stub_booru.calls, [])
        self.assertEqual(out[0][0], "chain")

    def test_search_keyword_goes_safebooru(self):
        p = make_plugin()
        stub_booru = _StubBooru()
        stub_sa = _StubSaucenao()
        stub_soutu = _StubSoutu()
        p.booru = stub_booru  # type: ignore[assignment]
        p.saucenao = stub_sa  # type: ignore[assignment]
        p.soutu = stub_soutu  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 cat_ears"), "cat_ears"))
        self.assertEqual(stub_booru.calls, ["cat_ears"])
        self.assertEqual(stub_sa.calls, [], "关键词分支不得调用 SauceNAO")
        self.assertEqual(stub_soutu.calls, [])
        self.assertEqual(out[0][0], "chain")

    def test_search_keyword_works_without_api_key(self):
        """未配置 api_key 时，「搜图 <关键词>」仍可正常走 Safebooru。"""
        p = make_plugin()  # saucenao_api_key 默认空
        stub_booru = _StubBooru()
        stub_sa = _StubSaucenao()
        p.booru = stub_booru  # type: ignore[assignment]
        p.saucenao = stub_sa  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 猫娘"), "猫娘"))
        self.assertEqual(stub_booru.calls, ["猫娘"], "无 key 也应能关键词搜图")
        self.assertEqual(len(stub_sa.calls), 0)
        self.assertEqual(out[0][0], "chain")

    def test_search_image_without_api_key_hints_and_runs_ascii2d(self):
        """未配置 api_key：不再只回引导 —— 跳过 SauceNAO，但仍运行 ascii2d。 [工程师已改 #11]"""
        p = make_plugin()  # 无 key
        stub_sa = _StubSaucenao()
        stub_a2d = _StubAscii2d()
        p.saucenao = stub_sa  # type: ignore[assignment]
        p.ascii2d = stub_a2d  # type: ignore[assignment]

        async def fe(event):
            return ImagePayload(data=PNG, mime="image/png", filename="q.png")

        p.image_source.from_event = fe  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图", with_image=True), ""))
        self.assertEqual(stub_sa.calls, [], "未配置 key 时不得发起 SauceNAO 请求")
        self.assertEqual(len(stub_a2d.calls), 1, "未配置 key 时仍应运行 ascii2d")
        self.assertEqual(out[0][0], "chain")
        text = _chain_text(out[0])
        self.assertIn(SAUCENAO_KEY_MISSING_TEXT, text)
        self.assertIn("ascii2d", text)

    def test_search_image_url_without_api_key_hints_and_runs_ascii2d(self):
        """未配置 api_key：图片链接仍会下载（供 ascii2d），跳过 SauceNAO。 [工程师已改 #12]"""
        p = make_plugin()  # 无 key
        stub_sa = _StubSaucenao()
        stub_a2d = _StubAscii2d()
        p.saucenao = stub_sa  # type: ignore[assignment]
        p.ascii2d = stub_a2d  # type: ignore[assignment]
        downloaded = []

        async def fs(src):
            downloaded.append(src)
            return ImagePayload(data=PNG, mime="image/png", filename="a.png")

        p.image_source.from_source = fs  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 http://x/a.jpg"), "http://x/a.jpg"))
        self.assertEqual(downloaded, ["http://x/a.jpg"], "为运行 ascii2d，图片链接仍需下载")
        self.assertEqual(stub_sa.calls, [])
        self.assertEqual(len(stub_a2d.calls), 1)
        self.assertEqual(out[0][0], "chain")
        self.assertIn(SAUCENAO_KEY_MISSING_TEXT, _chain_text(out[0]))

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
        self.assertIn("#搜图", text)
        self.assertNotIn("/搜图", text)
        self.assertNotIn("搜P站", text)
        self.assertEqual(_render_book_keyword_hint(""), BOOK_KEYWORD_NOT_SUPPORTED_TEXT.format(p="/"))
        self.assertEqual(_render_book_keyword_hint(None), BOOK_KEYWORD_NOT_SUPPORTED_TEXT.format(p="/"))

    def test_plugin_hints_use_actual_prefix(self):
        ctx = type("C", (), {"get_config": lambda self: {"wake_prefix": ["#"]}})()
        p = SoutuSearchPlugin(ctx, {})
        self.assertIn("#搜本", p._book_help_text())
        self.assertIn("#搜图", p._book_keyword_hint())

    def test_help_text_merges_auto_discrimination(self):
        """「搜图帮助」合并 SauceNAO + 关键词两条路，且不再提 `搜P站`。"""
        p = make_plugin()
        text = p._help_text()
        self.assertIn("SauceNAO", text)
        self.assertIn("Safebooru", text)
        self.assertIn("搜本", text)
        self.assertIn("/搜图帮助", text)
        self.assertNotIn("搜P站", text)
        self.assertNotIn("搜P站", HELP_TEXT)


# ===========================================================================
# 6. 访问控制覆盖四条指令（搜本 / 搜本帮助 / 搜图 / 搜图帮助）
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

    def test_sou_cmd_denied(self):
        p = self._denied_plugin()
        out = collect(p.sou_cmd(FakeEvent(umo="umo-A", text="/搜图"), ""))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)

    def test_sou_help_cmd_denied(self):
        p = self._denied_plugin()
        out = collect(p.sou_help_cmd(FakeEvent(umo="umo-A", text="/搜图帮助")))
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

    def test_sou_cmd_denied_zero_requests(self):
        """受限时「搜图」+ 图片必须零下载、零搜索（连 key 引导都不给）。"""
        p = make_plugin(access_mode="whitelist", whitelist=[])
        stub_sa = _StubSaucenao()
        p.saucenao = stub_sa  # type: ignore[assignment]
        fetched = []

        async def fake_from_event(event):
            fetched.append(1)
            return ImagePayload(data=PNG, mime="image/png", filename="q.png")

        p.image_source.from_event = fake_from_event  # type: ignore[assignment]

        async def fake_has_image(event):
            fetched.append("<has_image>")
            return True

        p.image_source.has_image = fake_has_image  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(umo="umo-A", text="/搜图"), ""))
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)
        self.assertEqual(fetched, [], "受限时应零结构检测 / 零取图")
        self.assertEqual(stub_sa.calls, [], "受限时应零搜索")


if __name__ == "__main__":
    unittest.main(verbosity=2)
