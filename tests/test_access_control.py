"""群/会话黑白名单访问控制测试（工程师）。

覆盖需求：
- ``access_mode=all``（默认）时 ``whitelist`` / ``blacklist`` 被完全忽略（向后兼容）
- ``access_mode=whitelist``：命中 / 未命中 / **空列表 fail-closed**
- ``access_mode=blacklist``：命中 / 未命中 / **空列表放行**
- 访问控制**仅作用于指令通道**：被拦的指令回一条提示
- 候选标识匹配：仅 group_id 命中 / 仅 umo 命中 / 都不命中
- 非法配置值回退

复用 tests/test_core.py 的 astrbot 桩（import 即完成 sys.modules 装配）。
运行::
    python -m unittest tests.test_access_control -v
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
    ACCESS_DENIED_TEXT,
    SoutuSearchPlugin,
)


def run(coro):
    return asyncio.run(coro)


def collect(agen):
    """收集异步生成器的所有产出。"""
    return run(_collect(agen))


async def _collect(agen):
    return [x async for x in agen]


class FakeEvent:
    """最小可用的事件替身。

    Args:
        umo: 会话唯一 ID（unified_msg_origin）。
        group_id: 群号，``None`` 表示不设置（私聊场景）。
        sender_id: 发送者 ID；``None`` 表示取不到（get_sender_id 抛异常）。
        text: 原始消息文本。
    """

    def __init__(self, umo="umo-A", group_id=None, sender_id=None, text="", message_id="m1"):
        self.unified_msg_origin = umo
        self.message_str = text
        msg = type("M", (), {})()
        if group_id is not None:
            msg.group_id = group_id
        msg.message_id = message_id
        self.message_obj = msg
        self._sender_id = sender_id

    def get_message_str(self):
        return self.message_str

    def get_sender_id(self):
        if self._sender_id is None:
            raise RuntimeError("sender id unavailable")
        return self._sender_id

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, comps):
        return ("chain", comps)


def make_plugin(**cfg):
    return SoutuSearchPlugin(object(), cfg)


# ===========================================================================
# 1. access_mode=all（默认）→ 忽略列表，向后兼容
# ===========================================================================
class TestAllModeIgnoresLists(unittest.TestCase):
    def test_default_config_allows_everything(self):
        p = make_plugin()
        ev = FakeEvent(umo="any-umo", group_id=999)
        self.assertTrue(p._is_access_allowed(ev))

    def test_lists_populated_still_ignored(self):
        # 填了白/黑名单，但模式是 all → 名录完全不生效
        p = make_plugin(access_mode="all", whitelist=["someone-else"], blacklist=["any-umo"])
        ev = FakeEvent(umo="any-umo", group_id=999)
        self.assertTrue(p._is_access_allowed(ev))


# ===========================================================================
# 2. whitelist 模式
# ===========================================================================
class TestWhitelistMode(unittest.TestCase):
    def test_hit_allowed(self):
        p = make_plugin(access_mode="whitelist", whitelist=["umo-A"])
        self.assertTrue(p._is_access_allowed(FakeEvent(umo="umo-A")))

    def test_miss_denied(self):
        p = make_plugin(access_mode="whitelist", whitelist=["umo-B"])
        self.assertFalse(p._is_access_allowed(FakeEvent(umo="umo-A")))

    def test_empty_list_fail_closed(self):
        """白名单为空 → fail-closed：全部会话都不可用（不能退化成放行）。"""
        for wl in ([], None, "", "not-a-list", {}):
            p = make_plugin(access_mode="whitelist", whitelist=wl)
            self.assertFalse(
                p._is_access_allowed(FakeEvent(umo="umo-A")),
                f"whitelist={wl!r} 应 fail-closed",
            )


# ===========================================================================
# 3. blacklist 模式
# ===========================================================================
class TestBlacklistMode(unittest.TestCase):
    def test_hit_denied(self):
        p = make_plugin(access_mode="blacklist", blacklist=["umo-A"])
        self.assertFalse(p._is_access_allowed(FakeEvent(umo="umo-A")))

    def test_miss_allowed(self):
        p = make_plugin(access_mode="blacklist", blacklist=["umo-B"])
        self.assertTrue(p._is_access_allowed(FakeEvent(umo="umo-A")))

    def test_empty_list_allows_all(self):
        """黑名单为空 = 不限制（刻意与白名单不对称）。"""
        for bl in ([], None, "", "not-a-list", {}):
            p = make_plugin(access_mode="blacklist", blacklist=bl)
            self.assertTrue(
                p._is_access_allowed(FakeEvent(umo="umo-A")),
                f"blacklist={bl!r} 应放行",
            )


# ===========================================================================
# 4. 受限时的回复行为（仅指令通道）
# ===========================================================================
class TestRestrictedBehavior(unittest.TestCase):
    def test_command_blocked_produces_one_prompt(self):
        """指令被拦 → 回一句 plain_result 提示。"""
        p = make_plugin(access_mode="blacklist", blacklist=["umo-A"])
        out = collect(p.sou_cmd(FakeEvent(umo="umo-A", text="/搜图"), ""))
        self.assertEqual(len(out), 1, "指令被拦应恰好回一条提示")
        self.assertEqual(out[0][0], "plain")
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)

    def test_help_command_blocked_produces_prompt(self):
        p = make_plugin(access_mode="whitelist", whitelist=[])
        out = collect(p.sou_help_cmd(FakeEvent(umo="umo-A", text="/搜图帮助")))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)


# ===========================================================================
# 5. 候选标识匹配
# ===========================================================================
class TestCandidateMatching(unittest.TestCase):
    def test_only_group_id_hit(self):
        p = make_plugin(access_mode="whitelist", whitelist=["10001"])
        ev = FakeEvent(umo="umo-X", group_id="10001")
        self.assertTrue(p._is_access_allowed(ev))

    def test_only_umo_hit(self):
        p = make_plugin(access_mode="whitelist", whitelist=["umo-X"])
        ev = FakeEvent(umo="umo-X", group_id="10001")
        self.assertTrue(p._is_access_allowed(ev))

    def test_neither_hit(self):
        p = make_plugin(access_mode="whitelist", whitelist=["nope"])
        ev = FakeEvent(umo="umo-X", group_id="10001")
        self.assertFalse(p._is_access_allowed(ev))

    def test_sender_id_hit(self):
        """私聊场景：仅提供发送者 ID 也可匹配。"""
        p = make_plugin(access_mode="whitelist", whitelist=["user-7"])
        ev = FakeEvent(umo="umo-private", group_id=None, sender_id="user-7")
        self.assertTrue(p._is_access_allowed(ev))

    def test_sender_id_unavailable_no_crash(self):
        p = make_plugin(access_mode="whitelist", whitelist=["user-7"])
        ev = FakeEvent(umo="umo-private", group_id=None, sender_id=None)
        self.assertFalse(p._is_access_allowed(ev))

    def test_number_and_whitespace_coercion(self):
        # 列表项带空白、群号为 int → 统一字符串化 + strip 后比对
        p = make_plugin(access_mode="whitelist", whitelist=["  10001  "])
        ev = FakeEvent(umo="umo-X", group_id=10001)
        self.assertTrue(p._is_access_allowed(ev))

    def test_empty_list_entries_ignored(self):
        # 列表中的空字符串 / None 应被忽略，不得误判为命中空候选
        p = make_plugin(access_mode="blacklist", blacklist=["", "  ", None])
        ev = FakeEvent(umo="umo-X", group_id="10001")
        self.assertTrue(p._is_access_allowed(ev), "空项应被忽略 → 黑名单视为空 → 放行")

    def test_no_group_id_private_chat(self):
        # 私聊无 group_id：不应报错，umo 命中即可
        p = make_plugin(access_mode="whitelist", whitelist=["umo-private"])
        ev = FakeEvent(umo="umo-private", group_id=None)
        self.assertTrue(p._is_access_allowed(ev))


# ===========================================================================
# 6. 非法配置值回退
# ===========================================================================
class TestInvalidConfigFallback(unittest.TestCase):
    def test_invalid_access_mode_falls_back_to_all(self):
        for bad in ("weird", "Whitelist", "", None, 123, "ALL"):
            p = make_plugin(access_mode=bad, whitelist=["umo-A"], blacklist=["umo-A"])
            self.assertEqual(p._access_mode(), "all", f"mode={bad!r} 应回退 all")
            # 回退 all 后，即便名录含本会话也应放行（列表被忽略）
            self.assertTrue(p._is_access_allowed(FakeEvent(umo="umo-A")))

    def test_valid_mode_preserved(self):
        p = make_plugin(access_mode="whitelist")
        self.assertEqual(p._access_mode(), "whitelist")


# ===========================================================================
# 7. 动态读取（配置变更后即时生效）
# ===========================================================================
class TestDynamicConfigRead(unittest.TestCase):
    def test_config_change_takes_effect(self):
        p = make_plugin(access_mode="all")
        ev = FakeEvent(umo="umo-A")
        self.assertTrue(p._is_access_allowed(ev))
        # 运行期改写 self.config → 判定应随之变化
        p.config["access_mode"] = "blacklist"
        p.config["blacklist"] = ["umo-A"]
        self.assertFalse(p._is_access_allowed(ev))


# ===========================================================================
# 8. P2-1：None / False / 0 等"空值"必须被剔除（否则 whitelist 无法 fail-closed）
# ===========================================================================
class TestP2InvalidEntriesSkipped(unittest.TestCase):
    def test_none_in_whitelist_fails_closed(self):
        """白名单 [None] 必须归一化为空集合 → fail-closed（不得因 str(None)=="None" 误命中）。"""
        p = make_plugin(access_mode="whitelist", whitelist=[None])
        self.assertEqual(p._whitelist(), set())
        self.assertFalse(p._is_access_allowed(FakeEvent(umo="None")))

    def test_none_false_zero_empty_all_skipped(self):
        self.assertEqual(
            SoutuSearchPlugin._as_id_set([None, False, True, 0, 0.0, "", "   "]),
            set(),
        )

    def test_real_values_kept_and_stripped(self):
        # int 123 → "123"；带空白字符串 strip 后保留
        self.assertEqual(SoutuSearchPlugin._as_id_set([123, " a "]), {"123", "a"})

    def test_candidate_side_same_normalization(self):
        # 候选侧与名单侧口径一致：group_id=0 / None 均跳过
        self.assertEqual(SoutuSearchPlugin._as_id_set([None]), set())


# ===========================================================================
# 9. P2-2：字符串形态名单按分隔符拆分；其它非法类型告警后置空
# ===========================================================================
class TestP2StringAndTypeHandling(unittest.TestCase):
    def test_blacklist_string_single(self):
        """误写成字符串的黑名单必须按本意生效（不得静默丢弃导致 fail-open）。"""
        p = make_plugin(access_mode="blacklist", blacklist="123456")
        self.assertEqual(p._blacklist(), {"123456"})
        self.assertFalse(p._is_access_allowed(FakeEvent(umo="123456")), "字符串黑名单应屏蔽 123456")

    def test_blacklist_string_multi(self):
        p = make_plugin(access_mode="blacklist", blacklist="123456, 789012")
        self.assertEqual(p._blacklist(), {"123456", "789012"})
        self.assertFalse(p._is_access_allowed(FakeEvent(umo="789012")))

    def test_whitelist_string_split(self):
        p = make_plugin(access_mode="whitelist", whitelist="umo-A\numo-B")
        self.assertEqual(p._whitelist(), {"umo-A", "umo-B"})
        self.assertTrue(p._is_access_allowed(FakeEvent(umo="umo-B")))

    def test_non_list_type_warns_and_empties(self):
        """非 list/str 类型（int）→ 打 warning 并按空处理（不再静默吞配置错误）。"""
        from unittest.mock import patch

        from astrbot_plugin_soutu_search import main as main_mod

        with patch.object(main_mod.logger, "warning") as warn:
            result = SoutuSearchPlugin._as_id_set(123456)
        self.assertEqual(result, set())
        self.assertTrue(warn.called, "非 list 类型应触发 logger.warning")

    def test_dict_type_warns_and_empties(self):
        from unittest.mock import patch

        from astrbot_plugin_soutu_search import main as main_mod

        with patch.object(main_mod.logger, "warning") as warn:
            result = SoutuSearchPlugin._as_id_set({"a": 1})
        self.assertEqual(result, set())
        self.assertTrue(warn.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
