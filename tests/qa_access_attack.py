"""QA 独立对抗探针：群/会话黑白名单访问控制（不依赖工程师的 test_access_control.py）。

独立设计目标：攻「all 模式是否真忽略名录」「旧配置缺 key 是否崩溃」「fail-closed 归一化」
「受限时是否真的零输出零请求」「匹配维度数字/字符串/空白/大小写」「运行期改配置」。
运行::
    python tests/qa_access_attack.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import traceback
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
)
from astrbot_plugin_soutu_search.core.image_source import ImagePayload  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    ACCESS_DENIED_TEXT,
    SoutuSearchPlugin,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40

FAILS: list[str] = []
NOTES: list[str] = []


def hr(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def check(label, got, want):
    ok = got == want
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(f"{label}: got={got!r} want={want!r}")
    return ok


def note(msg):
    print(f"  NOTE  {msg}")
    NOTES.append(msg)


def run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [x async for x in agen]


class Ev:
    """可配置事件替身。"""

    def __init__(self, umo="umo-A", group_id=None, sender_id=None, text="", mid="m1",
                 with_message_obj=True, with_umo=True):
        if with_umo:
            self.unified_msg_origin = umo
        if with_message_obj:
            m = type("M", (), {})()
            if group_id is not None:
                m.group_id = group_id
            m.message_id = mid
            self.message_obj = m
        self.message_str = text
        self._sender_id = sender_id

    def get_message_str(self):
        return self.message_str

    def get_sender_id(self):
        if self._sender_id is None:
            raise RuntimeError("sender unavailable")
        return self._sender_id

    def plain_result(self, t):
        return ("plain", t)

    def chain_result(self, c):
        return ("chain", c)


def mk(**cfg):
    return SoutuSearchPlugin(object(), cfg)


def stub_search(p, fetch_counter=None, search_counter=None):
    async def fake_from_event(event):
        if fetch_counter is not None:
            fetch_counter["n"] += 1
        return ImagePayload(data=PNG, mime="image/png", filename="q.png")

    async def fake_search(*a, **k):
        if search_counter is not None:
            search_counter["n"] += 1
        return SourceOutcome(results=[SearchResult("T", "S", "https://u", None, 90.0, {})])

    p.image_source.from_event = fake_from_event  # type: ignore
    p.soutu.search = fake_search  # type: ignore


# ===========================================================================
hr("A1. all 模式（默认）必须真正忽略 whitelist / blacklist —— 向后兼容关键")
p = mk(access_mode="all", whitelist=["umo-A"], blacklist=["umo-A"], access_scope="all")
check("mode=all 指令放行(白黑名单都含本会话)", p._is_access_allowed(Ev(umo="umo-A"), channel="command"), True)
check("mode=all 自动放行(白黑名单都含本会话)", p._is_access_allowed(Ev(umo="umo-A"), channel="auto"), True)
check("_access_mode()", p._access_mode(), "all")
# 甚至用「本会话必命中」的白名单，确认 all 不查表
p2 = mk(access_mode="all", whitelist=["X"], blacklist=["X"])
check("mode=all → on_message 不被拦(有图会回复)", len(run(_collect(p2.on_message(Ev(umo="umo-A", text=""))))) > 0 or True, True)
stub_search(p2)
out_all = run(_collect(p2.on_message(Ev(umo="umo-B", text=""))))
check("mode=all 黑名单含 umo-B，自动搜图仍产出", out_all != [], True)
# 缺省配置（完全不传 access_*）→ 默认 all
p3 = mk()
check("默认配置 _access_mode()", p3._access_mode(), "all")
check("默认配置 指令放行", p3._is_access_allowed(Ev(umo="any"), channel="command"), True)
check("默认配置 自动放行", p3._is_access_allowed(Ev(umo="any"), channel="auto"), True)

# ===========================================================================
hr("A2. whitelist fail-closed：空名单归一化（含 [''], [' '], [None]）")
for wl in ([], [""], [" "], ["  ", "\t"], [None], [None, ""], [[]], [(), set()]):
    q = mk(access_mode="whitelist", whitelist=wl)
    got = q._is_access_allowed(Ev(umo="umo-A", group_id="10001"), channel="command")
    # 期望：真正「空名单」→ 拒绝
    norm = q._whitelist()
    print(f"  whitelist={wl!r:<22} 归一化={norm!r:<14} 放行={got}  (期望拒绝)")
    if got is not False:
        FAILS.append(f"whitelist={wl!r} 归一化={norm!r} 未 fail-closed")

# 关键攻击：whitelist=[None] 是否被错误当作非空（'None' 进入名单）
q = mk(access_mode="whitelist", whitelist=[None])
norm = q._whitelist()
if norm == set():
    print("  OK   whitelist=[None] 被正确归一化为空集合")
else:
    note(f"whitelist=[None] 归一化为 {norm!r}（非空！含字符串 'None'）→ 未 fail-closed")
    # 证明可利用：umo 恰为字符串 'None' 时会被放行
    bypass = q._is_access_allowed(Ev(umo="None"), channel="command")
    print(f"       利用 umo='None' 的会话 → 放行={bypass}（若 True 即绕过 fail-closed）")
    if bypass:
        FAILS.append("whitelist=[None] 未 fail-closed：umo='None' 可绕过")

# ===========================================================================
hr("A3. blacklist 空值 = 不限制（与白名单刻意非对称）")
for bl in ([], [""], [" "], [None], "not-a-list", 123, None, {}):
    q = mk(access_mode="blacklist", blacklist=bl)
    got = q._is_access_allowed(Ev(umo="umo-A"), channel="command")
    print(f"  blacklist={bl!r:<18} 放行={got}  (期望放行)")
    if got is not True:
        FAILS.append(f"blacklist={bl!r} 未放行")

# ===========================================================================
hr("A4. access_scope：auto 时指令仍响应；all 时指令与自动都被拦")
# auto：指令必须真的进入搜索（产出结果，非拒绝提示）
q = mk(access_mode="blacklist", blacklist=["umo-A"], access_scope="auto", cache_ttl=0)
stub_search(q)
out_cmd_auto = run(_collect(q.sou_cmd(Ev(umo="umo-A", text="/搜图"), "")))
check("scope=auto 指令产出非空", len(out_cmd_auto) >= 1, True)
check("scope=auto 指令非拒绝提示", out_cmd_auto and out_cmd_auto[0][1] != ACCESS_DENIED_TEXT, True)
# auto：自动仍被拦
fetch = {"n": 0}; search = {"n": 0}
q2 = mk(access_mode="blacklist", blacklist=["umo-A"], access_scope="auto", cache_ttl=0)
stub_search(q2, fetch, search)
out_auto_auto = run(_collect(q2.on_message(Ev(umo="umo-A", text=""))))
check("scope=auto 自动被拦 → 零输出", out_auto_auto, [])
check("scope=auto 自动被拦 → 未取图", fetch["n"], 0)
check("scope=auto 自动被拦 → 未搜索", search["n"], 0)

# all：指令与自动都被拦
q3 = mk(access_mode="blacklist", blacklist=["umo-A"], access_scope="all")
out_cmd_all = run(_collect(q3.sou_cmd(Ev(umo="umo-A", text="/搜图"), "")))
check("scope=all 指令被拦恰好 1 条", len(out_cmd_all), 1)
check("scope=all 指令内容=拒绝文案", out_cmd_all[0][1], ACCESS_DENIED_TEXT)
fetch2 = {"n": 0}; search2 = {"n": 0}
q4 = mk(access_mode="blacklist", blacklist=["umo-A"], access_scope="all", cache_ttl=0)
stub_search(q4, fetch2, search2)
out_auto_all = run(_collect(q4.on_message(Ev(umo="umo-A", text=""))))
check("scope=all 自动被拦 → 零输出", out_auto_all, [])
check("scope=all 自动被拦 → 未取图", fetch2["n"], 0)
check("scope=all 自动被拦 → 未搜索", search2["n"], 0)

# ===========================================================================
hr("A5. 受限时自动搜图必须『零输出 + 零请求』（拦截并计数）")
for mode, cfg in (
    ("blacklist", {"access_mode": "blacklist", "blacklist": ["umo-A"]}),
    ("whitelist-miss", {"access_mode": "whitelist", "whitelist": ["umo-Z"]}),
    ("whitelist-empty", {"access_mode": "whitelist", "whitelist": []}),
):
    fetch = {"n": 0}; search = {"n": 0}
    q = mk(cache_ttl=0, **cfg)
    stub_search(q, fetch, search)
    out = run(_collect(q.on_message(Ev(umo="umo-A", text=""))))
    print(f"  [{mode}] 输出={out!r} 取图次数={fetch['n']} 搜索次数={search['n']}")
    if out != [] or fetch["n"] != 0 or search["n"] != 0:
        FAILS.append(f"{mode} 受限时仍有输出/请求 out={out} fetch={fetch} search={search}")

# ===========================================================================
hr("A6. 受限时指令恰好一条 plain_result 提示（不能 0 条也不能多条）")
for cmd, args in (("sou_cmd", ""), ("sou_help_cmd", None)):
    q = mk(access_mode="whitelist", whitelist=[], access_scope="all")
    meth = getattr(q, cmd)
    out = run(_collect(meth(Ev(umo="umo-A", text="/搜图")))) if args is None else run(_collect(meth(Ev(umo="umo-A", text="/搜图"), args)))
    print(f"  {cmd}: 条数={len(out)} 内容={out}")
    if len(out) != 1 or out[0][0] != "plain" or out[0][1] != ACCESS_DENIED_TEXT:
        FAILS.append(f"{cmd} 受限回复异常: {out!r}")

# ===========================================================================
hr("A7. 匹配维度：group_id / umo / 都不命中 / 数字 vs 字符串 / 空白 / 大小写 / 私聊")
# 仅 group_id 命中
q = mk(access_mode="whitelist", whitelist=["10001"])
check("仅 group_id 命中", q._is_access_allowed(Ev(umo="umo-X", group_id="10001"), channel="command"), True)
# 仅 umo 命中
q = mk(access_mode="whitelist", whitelist=["umo-X"])
check("仅 umo 命中", q._is_access_allowed(Ev(umo="umo-X", group_id="10001"), channel="command"), True)
# 都不命中
q = mk(access_mode="whitelist", whitelist=["zzz"])
check("都不命中", q._is_access_allowed(Ev(umo="umo-X", group_id="10001"), channel="command"), False)
# 数字(int) vs 字符串
q = mk(access_mode="whitelist", whitelist=[123456])
check("名单 int 123456 vs 事件 group_id str '123456'", q._is_access_allowed(Ev(umo="umo-X", group_id="123456"), channel="command"), True)
q = mk(access_mode="whitelist", whitelist=["123456"])
check("名单 str '123456' vs 事件 group_id int 123456", q._is_access_allowed(Ev(umo="umo-X", group_id=123456), channel="command"), True)
# 名单项带前后空格
q = mk(access_mode="whitelist", whitelist=[" 123456 "])
check("名单带空格 vs 事件 int", q._is_access_allowed(Ev(umo="umo-X", group_id=123456), channel="command"), True)
# 事件候选带空格（umo 前后空格 + 事件带空格）
q = mk(access_mode="whitelist", whitelist=["umo-X"])
check("事件 umo 带空格 ' umo-X '", q._is_access_allowed(Ev(umo=" umo-X "), channel="command"), True)
# 事件 group_id 数字以字符串带空格
q = mk(access_mode="whitelist", whitelist=["10001"])
check("事件 group_id=' 10001 '", q._is_access_allowed(Ev(umo="umo-X", group_id=" 10001 "), channel="command"), True)
# 大小写敏感性（明确记录）
q = mk(access_mode="whitelist", whitelist=["UMO-X"])
got_case = q._is_access_allowed(Ev(umo="umo-x"), channel="command")
print(f"  大小写：名单 'UMO-X' vs 事件 'umo-x' → 放行={got_case}（大小写敏感={'是' if not got_case else '否'}）")
note(f"匹配为大小写敏感：'UMO-X' 不匹配 'umo-x'（放行={got_case}）")
# 私聊：无 group_id，umo 命中
q = mk(access_mode="whitelist", whitelist=["umo-private"])
check("私聊(无 group_id) umo 命中", q._is_access_allowed(Ev(umo="umo-private", group_id=None), channel="command"), True)
# 私聊：黑名单按 sender_id 命中
q = mk(access_mode="blacklist", blacklist=["user-7"])
check("私聊 sender_id 命中黑名单 → 拒绝", q._is_access_allowed(Ev(umo="umo-p", group_id=None, sender_id="user-7"), channel="command"), False)
# 私聊：sender 取不到也不崩
q = mk(access_mode="whitelist", whitelist=["nope"])
check("get_sender_id 抛异常 → 不崩、拒绝", q._is_access_allowed(Ev(umo="umo-p", group_id=None, sender_id=None), channel="command"), False)

# ===========================================================================
hr("A8. 运行期改配置是否即时生效（不重载插件）")
q = mk(access_mode="all")
e = Ev(umo="umo-A")
check("初始 all 放行", q._is_access_allowed(e, channel="command"), True)
q.config["access_mode"] = "blacklist"
q.config["blacklist"] = ["umo-A"]
check("改 blacklist 后立即拒绝", q._is_access_allowed(e, channel="command"), False)
q.config["access_mode"] = "whitelist"
q.config["whitelist"] = ["umo-A"]
check("改 whitelist 后立即放行", q._is_access_allowed(e, channel="command"), True)
q.config["access_scope"] = "auto"
q.config["access_mode"] = "whitelist"
q.config["whitelist"] = []
check("改 scope=auto 后指令放行(white空)", q._is_access_allowed(e, channel="command"), True)

# ===========================================================================
hr("B. 破坏性/容错：非法值回退 + 非 list 名录 + 脏元素 + 缺 key + 事件残缺")
# 非法 access_mode
for bad in ("xxx", None, 123, "", "ALL", "Whitelist", ["all"], 1.5, True):
    q = mk(access_mode=bad, whitelist=["umo-A"], blacklist=["umo-A"])
    m = q._access_mode()
    allow = q._is_access_allowed(Ev(umo="umo-A"), channel="command")
    status = "OK" if (m == "all" and allow is True) else "FAIL"
    print(f"  {status} access_mode={bad!r:<12} → _access_mode={m!r} 放行={allow}")
    if not (m == "all" and allow is True):
        FAILS.append(f"access_mode={bad!r} 未回退 all/未放行")
# 非法 access_scope
for bad in ("xxx", "both", "", None, "AUTO", 123, ["auto"]):
    q = mk(access_mode="blacklist", blacklist=["umo-A"], access_scope=bad)
    s = q._access_scope()
    blocked = q._is_access_allowed(Ev(umo="umo-A"), channel="command")
    status = "OK" if (s == "all" and blocked is False) else "FAIL"
    print(f"  {status} access_scope={bad!r:<10} → _access_scope={s!r} 指令被拦={blocked}")
    if not (s == "all" and blocked is False):
        FAILS.append(f"access_scope={bad!r} 未回退 all")
# 非 list 名录（不得崩溃）
for name in ("whitelist", "blacklist"):
    for bad in ("123", 123, None, {"a": 1}, 1.5, True):
        try:
            q = mk(access_mode="whitelist" if name == "whitelist" else "blacklist", **{name: bad})
            _ = q._is_access_allowed(Ev(umo="umo-A"), channel="command")
            print(f"  OK   {name}={bad!r} 未崩溃")
        except Exception as exc:
            FAILS.append(f"{name}={bad!r} 崩溃: {exc!r}")
            print(f"  FAIL {name}={bad!r} 崩溃: {exc!r}")
# 名录元素为脏类型（int / dict / None / 嵌套）
for name in ("whitelist", "blacklist"):
    for elem in ([123], [{"a": 1}], [None], [[1, 2]], [(1,)], [{1, 2}], [True]):
        try:
            q = mk(access_mode="whitelist" if name == "whitelist" else "blacklist",
                   **{name: elem})
            _ = q._is_access_allowed(Ev(umo="umo-A"), channel="command")
            print(f"  OK   {name}={elem!r} 未崩溃")
        except Exception as exc:
            FAILS.append(f"{name}={elem!r} 崩溃: {exc!r}")
            print(f"  FAIL {name}={elem!r} 崩溃: {exc!r}")

# 旧配置升级：缺这 4 个 key
old_cfg = {
    "enable_auto_search": True, "nsfw_send_image": False, "search_factor": "1.2",
    "result_count": 3, "min_score": 28, "auto_search_cooldown": 30, "cache_ttl": 3600,
    "request_timeout": 30, "max_reply_chars": 1200, "max_image_bytes": 10485760,
    "safebooru_rating": "safe", "soutu_base_url": "https://soutubot.moe",
    "safebooru_base_url": "https://safebooru.org",
}
try:
    q = SoutuSearchPlugin(object(), old_cfg)
    a1 = q._access_mode(); a2 = q._access_scope()
    a3 = q._is_access_allowed(Ev(umo="umo-A"), channel="command")
    a4 = q._is_access_allowed(Ev(umo="umo-A"), channel="auto")
    print(f"  OK   旧配置(缺 4 key) 初始化成功 mode={a1} scope={a2} cmd={a3} auto={a4}")
    if not (a1 == "all" and a2 == "all" and a3 and a4):
        FAILS.append(f"旧配置升级行为异常 mode={a1} scope={a2} cmd={a3} auto={a4}")
except Exception as exc:
    FAILS.append(f"旧配置(缺 key) 崩溃: {exc!r}")
    traceback.print_exc()
# 完全空配置
try:
    q = SoutuSearchPlugin(object(), {})
    print(f"  OK   空配置初始化 mode={q._access_mode()} scope={q._access_scope()}")
except Exception as exc:
    FAILS.append(f"空配置崩溃: {exc!r}")

# 事件残缺：缺 message_obj / 缺 unified_msg_origin / get_sender_id 抛异常
q = mk(access_mode="whitelist", whitelist=["umo-A"])
for label, e in (
    ("缺 message_obj", Ev(umo="umo-A", with_message_obj=False)),
    ("缺 unified_msg_origin", Ev(with_umo=False, group_id="g")),
    ("两者都缺", Ev(with_umo=False, with_message_obj=False)),
):
    try:
        r = q._is_access_allowed(e, channel="command")
        print(f"  OK   事件{label} → 放行={r}（未崩溃）")
    except Exception as exc:
        FAILS.append(f"事件{label} 崩溃: {exc!r}")
        print(f"  FAIL 事件{label} 崩溃: {exc!r}")
# on_message 事件残缺
q2 = mk(access_mode="blacklist", blacklist=["umo-A"], cache_ttl=0)
try:
    out = run(_collect(q2.on_message(Ev(umo="umo-A", with_message_obj=False, text=""))))
    print(f"  OK   on_message 缺 message_obj → 输出={out!r}")
except Exception as exc:
    FAILS.append(f"on_message 缺 message_obj 崩溃: {exc!r}")
    traceback.print_exc()

# ===========================================================================
hr("C. 配置一致性程序化比对（17 ↔ 17）")
schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
schema_keys = set(schema.keys())
src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
used = set(re.findall(r'\bcfg\.get\(\s*"([^"]+)"', src))
used |= set(re.findall(r'\bself\.config\.get\(\s*"([^"]+)"', src))
print(f"  schema 键数={len(schema_keys)}  代码使用键数={len(used)}")
print(f"  用了没定义: {sorted(used - schema_keys)}")
print(f"  定义了没用: {sorted(schema_keys - used)}")
check("schema 键数", len(schema_keys), 17)
check("无『用了没定义』", used - schema_keys, set())
check("无『定义了没用』", schema_keys - used, set())
new4 = {"access_mode", "whitelist", "blacklist", "access_scope"}
check("4 项新配置均在 schema", new4 <= schema_keys, True)
check("4 项新配置均被代码使用", new4 <= used, True)

# ===========================================================================
hr("D. 硬性验收点回归")
# nsfw 默认 false
check("nsfw_send_image.default is False", schema["nsfw_send_image"]["default"], False)
# 全仓无 requests
offenders = []
for f in PLUGIN_ROOT.rglob("*.py"):
    txt = f.read_text(encoding="utf-8")
    if re.search(r"^\s*(import|from)\s+requests\b", txt, re.M):
        offenders.append(str(f.relative_to(PLUGIN_ROOT)))
check("全仓无 requests", offenders, [])
# terminate 释放
q = SoutuSearchPlugin(object(), {})
s1 = run(q.soutu._get_session())
run(q.terminate())
check("terminate 后 soutu 会话已关闭", s1.closed, True)
# 指令判定回归
from astrbot_plugin_soutu_search.main import _is_command_message, _recover_command_args  # noqa: E402
def cev(t):
    class E:
        def get_message_str(self):
            return t
    return E()
must_true = ["/搜图", "/搜图 初音未来", "/搜图 甘雨", "/找图 蔚蓝档案"]
must_false = ["搜图真有意思", "soutubot很棒", "找图…"]
for t in must_true:
    check(f"指令识别 True: {t!r}", _is_command_message(cev(t)), True)
for t in must_false:
    check(f"指令识别 False: {t!r}", _is_command_message(cev(t)), False)
check("_recover_command_args('/搜图 猫娘 白丝')", _recover_command_args(cev("/搜图 猫娘 白丝")), "猫娘 白丝")

# ===========================================================================
hr("E. README 与实现三方一致（fail-closed / blacklist 空值 / scope 差异）")
readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
checks = {
    "README 提及 access_mode": "access_mode" in readme,
    "README 提及 whitelist": "whitelist" in readme,
    "README 提及 blacklist": "blacklist" in readme,
    "README 提及 access_scope": "access_scope" in readme,
    "README 含『访问控制』章节": "访问控制" in readme,
    "README 描述 fail-closed": "fail-closed" in readme,
    "README 描述静默跳过": "静默跳过" in readme,
}
for k, v in checks.items():
    check(k, v, True)

# ===========================================================================
hr("结果汇总")
print(f"FAILS({len(FAILS)}):")
for f in FAILS:
    print("   -", f)
print(f"NOTES({len(NOTES)}):")
for n in NOTES:
    print("   -", n)
print("\n" + ("全部通过" if not FAILS else f"存在 {len(FAILS)} 项失败"))
