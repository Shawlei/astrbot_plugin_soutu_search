"""命令前缀跟随 AstrBot 全局配置（顶层 ``wake_prefix``）测试。

覆盖：
- ``_normalize_prefixes``：``str`` / ``list`` / ``tuple`` / ``set`` / 非法类型 / 空值 / 混入非字符串项；
- ``_strip_wake_prefix``：优先按配置前缀（``str.startswith``，不受正则元字符影响）剥离，未命中回退通用正则；
- ``_command_head`` / ``_recover_command_args`` 在配置前缀下的判定与参数还原；
- 插件 ``_wake_prefixes()`` 从 ``self.context.get_config()`` 读取顶层 ``wake_prefix`` 的容错；
- 帮助文案按实际前缀渲染（端到端 ``sou_help_cmd``）；
- 向后兼容：默认 ``/`` 前缀行为不变。

复用 tests/test_core.py 的 astrbot 桩（import 即完成 sys.modules 装配）。
运行::
    python -m unittest tests.test_wake_prefix -v
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

from astrbot_plugin_soutu_search.main import (  # noqa: E402
    DEFAULT_WAKE_PREFIX,
    HELP_TEXT,
    SoutuSearchPlugin,
    _command_head,
    _normalize_prefixes,
    _recover_command_args,
    _render_help,
    _strip_wake_prefix,
)


def run(coro):
    return asyncio.run(coro)


class _Ctx:
    """最小 context 替身：只实现 get_config()。"""

    def __init__(self, cfg):
        self._cfg = cfg

    def get_config(self):
        return self._cfg


class _RaisingCtx:
    def get_config(self):
        raise RuntimeError("boom")


class _Ev:
    def __init__(self, text=""):
        self.unified_msg_origin = "umo-A"
        self.message_str = text
        self.message_obj = type("M", (), {"message_id": "m1"})()

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, comps):
        return ("chain", comps)


async def _collect(agen):
    return [x async for x in agen]


def collect(agen):
    return run(_collect(agen))


# ===========================================================================
# 1. _normalize_prefixes
# ===========================================================================
class TestNormalizePrefixes(unittest.TestCase):
    def test_str_single(self):
        self.assertEqual(_normalize_prefixes("#"), ["#"])
        self.assertEqual(_normalize_prefixes("/"), ["/"])

    def test_list_multiple(self):
        self.assertEqual(_normalize_prefixes(["/", "#"]), ["/", "#"])

    def test_tuple_and_set(self):
        self.assertEqual(_normalize_prefixes(("/", "!")), ["/", "!"])
        self.assertEqual(sorted(_normalize_prefixes({"?", "/"})), sorted(["?", "/"]))

    def test_empty_values(self):
        for bad in (None, [], (), set(), "", "   "):
            self.assertEqual(_normalize_prefixes(bad), [], f"{bad!r} 应为空列表")

    def test_invalid_types(self):
        for bad in (123, {"a": 1}, True, 4.5, b"/"):
            self.assertEqual(_normalize_prefixes(bad), [], f"{bad!r} 应为空列表")

    def test_mixed_list_skips_non_strings(self):
        self.assertEqual(_normalize_prefixes([None, 1, "-", "", "  ", "/"]), ["-", "/"])


# ===========================================================================
# 2. _strip_wake_prefix
# ===========================================================================
class TestStripWakePrefix(unittest.TestCase):
    def test_config_prefix_preferred(self):
        self.assertEqual(_strip_wake_prefix("#搜图", ["#"]), "搜图")

    def test_multiple_prefixes(self):
        for p in ("/", "!", "#"):
            self.assertEqual(_strip_wake_prefix(f"{p}搜图", ["/", "!", "#"]), "搜图")

    def test_regex_metachar_prefix_safe(self):
        """.(/ 等正则元字符前缀必须被安全剥离（不拼进正则）。"""
        self.assertEqual(_strip_wake_prefix(".( 搜图", [".(", "?"]), " 搜图")
        self.assertEqual(_strip_wake_prefix(".(搜图", [".("]), "搜图")

    def test_word_prefix_stripped(self):
        """单词形态前缀（普通字母/数字等 \\w 字符）也能剥离——通用正则做不到，配置前缀可以。"""
        self.assertEqual(_strip_wake_prefix("q搜图", ["q"]), "搜图")
        self.assertEqual(_strip_wake_prefix("搜图", ["搜图"]), "")

    def test_fallback_to_generic_regex_when_no_match(self):
        # 配置前缀是 #，但消息用 / → 回退通用正则仍能剥离
        self.assertEqual(_strip_wake_prefix("/搜图", ["#"]), "搜图")

    def test_no_change_when_no_prefix(self):
        self.assertEqual(_strip_wake_prefix("搜图", ["#"]), "搜图")


# ===========================================================================
# 3. 指令判定 / 参数还原（配置前缀）
# ===========================================================================
class TestCommandWithPrefix(unittest.TestCase):
    def test_command_head_with_hash(self):
        self.assertEqual(_command_head("#搜图", ["#"]), "搜图")
        self.assertEqual(_command_head("#搜图 猫娘", ["#"]), "搜图")
        self.assertEqual(_command_head("#搜图帮助", ["#"]), "搜图帮助")
        self.assertEqual(_command_head("#soutuhelp", ["#"]), "soutuhelp")

    def test_command_head_non_command_with_hash(self):
        for t in ("#搜索图片", "#普通聊天", "#帮我搜图"):
            self.assertIsNone(_command_head(t, ["#"]), f"不应识别为指令: {t!r}")

    def test_word_prefix_only_with_config(self):
        # 单词前缀：配置前缀下可识别；无配置回退正则则识别不了
        self.assertEqual(_command_head("q搜图", ["q"]), "搜图")
        self.assertIsNone(_command_head("q搜图"))

    def test_recover_args_with_hash(self):
        ev = _Ev("#搜图 猫娘 白丝")
        self.assertEqual(_recover_command_args(ev, ["#"]), "猫娘 白丝")

    def test_recover_args_default_still_works(self):
        ev = _Ev("/搜图 cat_ears")
        self.assertEqual(_recover_command_args(ev), "cat_ears")


# ===========================================================================
# 4. 插件读取 wake_prefix 的容错
# ===========================================================================
class TestPluginWakePrefixes(unittest.TestCase):
    def _plugin(self, ctx):
        return SoutuSearchPlugin(ctx, {})

    def test_reads_list(self):
        p = self._plugin(_Ctx({"wake_prefix": ["#"]}))
        self.assertEqual(p._wake_prefixes(), ["#"])

    def test_reads_str(self):
        p = self._plugin(_Ctx({"wake_prefix": "#"}))
        self.assertEqual(p._wake_prefixes(), ["#"])

    def test_reads_multiple(self):
        p = self._plugin(_Ctx({"wake_prefix": ["/", "!"]}))
        self.assertEqual(p._wake_prefixes(), ["/", "!"])

    def test_none_and_empty_fall_back(self):
        for bad in (None, [], "", "   "):
            p = self._plugin(_Ctx({"wake_prefix": bad}))
            self.assertEqual(p._wake_prefixes(), [], f"wake_prefix={bad!r} 应为空（回退正则）")

    def test_invalid_types_fall_back(self):
        for bad in (123, {"a": 1}, True):
            p = self._plugin(_Ctx({"wake_prefix": bad}))
            self.assertEqual(p._wake_prefixes(), [], f"wake_prefix={bad!r} 应为空（回退正则）")

    def test_mixed_list_skips_non_strings(self):
        p = self._plugin(_Ctx({"wake_prefix": [None, 1, "#"]}))
        self.assertEqual(p._wake_prefixes(), ["#"])

    def test_missing_key(self):
        p = self._plugin(_Ctx({}))
        self.assertEqual(p._wake_prefixes(), [])

    def test_get_config_raises(self):
        p = self._plugin(_RaisingCtx())
        self.assertEqual(p._wake_prefixes(), [], "get_config 抛异常应回退空列表且不崩")

    def test_get_config_returns_non_dict(self):
        p = self._plugin(_Ctx(["not", "a", "dict"]))
        self.assertEqual(p._wake_prefixes(), [])

    def test_context_without_get_config(self):
        p = self._plugin(object())
        self.assertEqual(p._wake_prefixes(), [])

    def test_help_prefix_first_or_default(self):
        self.assertEqual(self._plugin(_Ctx({"wake_prefix": ["#", "!"]}))._help_prefix(), "#")
        self.assertEqual(self._plugin(_Ctx({}))._help_prefix(), DEFAULT_WAKE_PREFIX)


# ===========================================================================
# 5. 帮助文案按实际前缀渲染（端到端）
# ===========================================================================
class TestHelpRendering(unittest.TestCase):
    def test_render_help_with_prefix(self):
        text = _render_help("#")
        self.assertIn("#搜图", text)
        self.assertIn("#搜图帮助", text)
        self.assertNotIn("/搜图", text)

    def test_render_help_empty_falls_back(self):
        self.assertIn("/搜图", _render_help(""))

    def test_default_help_constant(self):
        self.assertIn("/搜图", HELP_TEXT)

    def test_help_text_end_to_end_hash(self):
        p = SoutuSearchPlugin(_Ctx({"wake_prefix": "#"}), {})
        self.assertIn("#搜图", p._help_text())

        out = collect(p.sou_help_cmd(_Ev("#搜图帮助")))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "plain")
        self.assertIn("#搜图帮助", out[0][1])

    def test_help_text_end_to_end_default(self):
        p = SoutuSearchPlugin(_Ctx({}), {})
        self.assertIn("/搜图", p._help_text())
        out = collect(p.sou_help_cmd(_Ev("/搜图帮助")))
        self.assertIn("/搜图帮助", out[0][1])


# ===========================================================================
# 6. 向后兼容：默认 / 前缀
# ===========================================================================
class TestBackwardCompat(unittest.TestCase):
    def test_default_slash_still_command(self):
        for t in ("/搜图", "/搜图 cat", "/soutuhelp", "搜图帮助x", "。搜图 x"):
            self.assertIsNotNone(_command_head(t), f"应识别为指令: {t!r}")

    def test_default_non_commands(self):
        for t in ("搜图真有意思", "soutubot很棒", "普通聊天", "帮我搜图", "搜索图片", "/其它指令"):
            self.assertIsNone(_command_head(t), f"不应识别为指令: {t!r}")


# ===========================================================================
# 7. 重复前缀 / 兜底正则不回归（P2-E1）
# ===========================================================================
class TestRepeatedPrefixStripping(unittest.TestCase):
    """命中配置前缀后仍须再走一次贪婪正则，吃光连续重复前缀（旧版行为不回归）。"""

    def test_strip_repeated_slash(self):
        self.assertEqual(_strip_wake_prefix("//搜图", ["/"]), "搜图")
        self.assertEqual(_strip_wake_prefix("///搜图", ["/"]), "搜图")

    def test_strip_repeated_hash(self):
        self.assertEqual(_strip_wake_prefix("##搜图", ["#"]), "搜图")

    def test_command_head_repeated_prefix(self):
        self.assertEqual(_command_head("//搜图", ["/"]), "搜图")
        self.assertEqual(_command_head("##搜图", ["#"]), "搜图")
        self.assertEqual(_command_head("##搜图帮助", ["#"]), "搜图帮助")

    def test_command_head_repeated_prefix_no_config(self):
        # 无配置（空列表）同样走兜底正则
        self.assertEqual(_command_head("//搜图", []), "搜图")
        self.assertEqual(_command_head("##搜图", []), "搜图")
        self.assertEqual(_command_head("//搜图"), "搜图")

    def test_recover_args_repeated_prefix(self):
        ev = _Ev("//搜图 猫娘 白丝")
        self.assertEqual(_recover_command_args(ev, ["/"]), "猫娘 白丝")
        ev2 = _Ev("##搜图 cat_ears")
        self.assertEqual(_recover_command_args(ev2, ["#"]), "cat_ears")

    def test_strip_repeated_mixed_prefix(self):
        # 混杂的非单词前缀一并吃光
        self.assertEqual(_strip_wake_prefix("。。搜图", ["/"]), "搜图")
        self.assertEqual(_strip_wake_prefix("..搜图", []), "搜图")

    # ---- 语义不能变（防回退时把「中文关键词 vs 人话连读」判坏）----
    def test_semantics_unchanged_human_continuation(self):
        self.assertIsNone(_command_head("#搜图真有意思", ["#"]))
        self.assertIsNone(_command_head("##搜图真有意思", ["#"]))

    def test_semantics_unchanged_command_with_cjk_arg(self):
        self.assertEqual(_command_head("/搜图 猫娘", ["/"]), "搜图")
        self.assertEqual(_command_head("//搜图 猫娘", ["/"]), "搜图")

    def test_semantics_unchanged_attached_cjk_tag(self):
        # `#搜索图片`：head=搜图 但紧跟 '索'(CJK) → 非指令
        self.assertIsNone(_command_head("#搜索图片", ["#"]))
        self.assertIsNone(_command_head("#搜索图片"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
