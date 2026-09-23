"""QA 第三轮独立复验（P1 SSRF + 3×P2 的修复）。

重点：
- #6 DNS 层与重定向链绕过（含 fake session 模拟 302 到内网 / 到 file: / 超跳数）
- #2 截断极值
- #5 紧贴识别 + 误报消除 + **回归：CJK 关键词参数还原是否被 isascii 规则破坏**
- #5b message_id 缺失回退键
运行::
    python tests/qa_reverify_r3.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401

from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.image_source import ImageSource  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    SoutuSearchPlugin,
    _command_head,
    _recover_command_args,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PUB1 = "http://93.184.216.34/a.jpg"   # 公网字面量（无需 DNS）
PUB2 = "http://93.184.216.35/b.jpg"


def run(c):
    return asyncio.run(c)


def hr(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


# --------------------------------------------------------------------------- #
hr("#6 DNS/混淆 IP：所有已知绕过向量应被拦截（fail-closed）")
VECTORS = ["2130706433", "0x7f000001", "0177.0.0.1", "127.1", "127.0.0.1.nip.io",
           "127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "localhost",
           "::1", "::ffff:127.0.0.1", "0.0.0.0"]
gaps = []
for h in VECTORS:
    b = ImageSource._host_blocked(h)
    if not b:
        gaps.append(h)
    print(f"  {'OK ' if b else '>>> GAP'} _host_blocked({h!r}) = {b}")
print("GAP 数量:", len(gaps), gaps)

# 公网不应被误杀（避免 fail-closed 误伤正常图床）
for h in ["safebooru.org", "soutubot.moe", "gchat.qpic.cn"]:
    try:
        b = ImageSource._host_blocked(h)
        print(f"  公网 {h:<18} blocked={b} (期望 False)")
    except Exception as e:
        print(f"  公网 {h:<18} 异常 {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
hr("#6b 重定向链：每跳重校验（fake session，无真实网络）")
class _Resp:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._b = body

    async def read(self):
        return self._b

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, mapping):
        self.mapping = mapping
        self.closed = False

    def get(self, url, **kw):
        return self.mapping.get(url, _Resp(500))


def _src(mapping):
    s = ImageSource(timeout=5)
    s._session = _Session(mapping)
    return s


cases = [
    ("302->内网", {PUB1: _Resp(302, {"Location": "http://127.0.0.1/x.jpg"})}),
    ("302->file:", {PUB1: _Resp(302, {"Location": "file:///etc/passwd"})}),
    ("302 无 Location", {PUB1: _Resp(302, {})}),
    ("超跳数", {PUB1: _Resp(302, {"Location": PUB2}),
                PUB2: _Resp(302, {"Location": PUB1})}),
]
for name, mp in cases:
    try:
        run(_src(mp).fetch_url(PUB1))
        print(f"  >>> GAP {name}: 未拦截")
    except Exception as e:
        print(f"  OK  {name}: {type(e).__name__}: {str(e)[:60]}")

# 正常：302 -> 公网 -> 200 图片
ok_map = {PUB1: _Resp(302, {"Location": PUB2}), PUB2: _Resp(200, body=PNG)}
try:
    data = run(_src(ok_map).fetch_url(PUB1))
    print(f"  OK  302->公网->200 返回 {len(data)} 字节（放行正常跳转）")
except Exception as e:
    print(f"  >>> GAP 正常跳转被误拦: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
hr("#2 截断极值（0..1200 全应 <= max_chars）")
results = [SearchResult(title="标" * 400, source="NH本子",
                        url="https://nhentai.net/g/123456/999/", score=99.9, extra={})
           for _ in range(20)]
bad = []
for mc in (0, -1, 1, 5, 10, 11, 12, 15, 50, 100, 300, 1200, 4500):
    blocks = format_outcome(SourceOutcome(results=results), nsfw_send_image=False,
                            max_results=20, header="H", max_chars=mc)
    text = "\n".join(b.get("text", "") for b in blocks)
    ok = (mc <= 0) or (len(text) <= mc)
    if not ok:
        bad.append((mc, len(text)))
    print(f"  max_chars={mc:>5} -> {len(text):>5} 字 合规={ok} 含后缀={'已截断' in text}")
print("越界:", bad)


# --------------------------------------------------------------------------- #
hr("#5 指令判定：紧贴识别 / 误报 / 【回归】CJK 参数还原")
def ev(t):
    class E:
        def get_message_str(self):
            return t
    return E()


print("-- 应识别为指令 --")
for t in ["/搜本", "/搜本子", "/搜本 猫娘", "/搜本 http://a.com/x.jpg", "/搜本帮助",
          "/soutuhelp", "/soutu 猫娘", "/找图 猫娘",
          "/搜图", "/搜图cat", "/搜图http://x", "搜图帮助x", "。搜图 x", "!找图"]:
    print(f"   {(_command_head(t) is not None)!s:<5} <- {t!r}")
print("-- 应【不】识别（误报） --")
for t in ["搜本真好看", "搜本子真好看", "搜图真有意思", "soutubot很棒", "找图…",
          "搜图帮助…", "普通聊天", "/其它指令"]:
    print(f"   {(_command_head(t) is not None)!s:<5} <- {t!r}")

print("-- 【回归】非 ASCII 参数还原（_recover_command_args） --")
for t in ["/搜本 猫娘", "/搜本 猫娘 白丝", "/soutu 猫娘", "/搜本子", "/搜本帮助",
          "/搜图 cat_ears", "/搜图 blue_archive", "/搜图 猫娘", "/搜图 猫娘 白丝",
          "/搜图帮助"]:
    head = _command_head(t)
    rec = _recover_command_args(ev(t))
    print(f"   head={head!r:<8} recovered={rec!r:<18} <- {t!r}")


# --------------------------------------------------------------------------- #
hr("#5b 自动搜图已彻底删除（无 on_message / 无判重登记）")
_main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
print("  main.py 仍有 on_message 监听器:", "def on_message" in _main_src)
print("  main.py 仍有 _RecentMessageRegistry:", "_RecentMessageRegistry" in _main_src)

print("\n完成。")
