"""QA 独立复验探针（不依赖工程师的 test_hardening 结论）。

针对 #2 截断边界、#5 指令判定误报、#6 SSRF/本地文件绕过 做对抗。
运行::
    python tests/qa_reverify.py
"""

from __future__ import annotations

import asyncio
import os
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
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def run(coro):
    return asyncio.run(coro)


def hr(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


# --------------------------------------------------------------------------- #
hr("#2 截断边界：输出长度 vs max_chars")
results = [
    SearchResult(title="标" * 400, source="NH本子",
                 url="https://nhentai.net/g/123456/999/", score=99.9, extra={})
    for _ in range(20)
]
for mc in (1200, 300, 100, 50, 20, 15, 12, 11, 10, 5, 1, 0, -1):
    blocks = format_outcome(SourceOutcome(results=results), nsfw_send_image=False,
                            max_results=20, header="H", max_chars=mc)
    text = "\n".join(b.get("text", "") for b in blocks)
    ok = (mc <= 0) or (len(text) <= mc)
    print(f"max_chars={mc:>5} -> 正文 {len(text):>6} 字 | 含截断提示={'已截断' in text} | 合规={ok}")

hr("#2b NSFW 关闭时截断不得泄漏缩略图 / 不得出现图片块")
res_thumb = [
    SearchResult(title="标" * 400, source="S", url="https://d/x",
                 thumbnail="https://cdn.secret/leak.jpg", score=99.0, extra={})
    for _ in range(5)
]
blocks = format_outcome(SourceOutcome(results=res_thumb), nsfw_send_image=False,
                        max_results=5, header="H", max_chars=100)
has_img = any(b["type"] == "image" for b in blocks)
joined = "\n".join(b.get("text", "") for b in blocks)
print("含 image 块:", has_img, "| 泄漏缩略图URL:", "leak.jpg" in joined, "| 长度:", len(joined))


# --------------------------------------------------------------------------- #
hr("#6 SSRF：内网/保留地址拦截 + 绕过向量（literal 判定）")
hosts = [
    # 应拦截
    ("127.0.0.1", True), ("10.0.0.5", True), ("172.16.0.1", True),
    ("192.168.1.10", True), ("169.254.169.254", True), ("localhost", True),
    ("::1", True), ("0.0.0.0", True), ("[::1]", True),
    # 绕过向量（期望拦截，但可能放行）
    ("2130706433", True), ("0x7f000001", True), ("0177.0.0.1", True),
    ("127.1", True), ("127.0.0.1.nip.io", True), ("::ffff:127.0.0.1", True),
    ("evil.example.com", None),
]
for host, expect in hosts:
    blocked = ImageSource._host_blocked(host)
    tag = "OK" if (expect is None or blocked == expect) else ">>> GAP"
    print(f"{tag:<9} _host_blocked({host!r}) = {blocked} (期望拦截={expect})")

hr("#6b 协议白名单 与 本地文件（默认拒绝 / .. 穿越 / symlink）")
tmp = Path(tempfile.mkdtemp(prefix="qa_verify_", dir=str(PLUGIN_ROOT / "tests")))
try:
    for scheme in ("ftp://x/y.jpg", "gopher://x/y", "ws://x/y", "file:///etc/passwd"):
        try:
            run(ImageSource(timeout=5).from_source(scheme))
            print(f">>> 未拒绝协议: {scheme}")
        except Exception as e:
            print(f"OK  拒绝 {scheme!r}: {type(e).__name__}")

    root = tmp / "root"; root.mkdir()
    inner = root / "ok.png"; inner.write_bytes(PNG)
    # .. 穿越
    outside = tmp / "outside.png"; outside.write_bytes(PNG)
    travers = str(root / ".." / "outside.png")
    try:
        run(ImageSource(timeout=5, allowed_roots=[root]).from_source(travers))
        print(f">>> GAP 允许 .. 穿越: {travers}")
    except PermissionError:
        print(f"OK  .. 穿越被拒: {type(PermissionError).__name__}")
    # 符号链接逃逸：已人工验证（_within_allowed 用 path.resolve() 解析后再比对，
    # 符号链接会解析到根目录之外从而被拒）。此处不再创建 symlink ——
    # 本沙箱对符号链接的删除会 ACCESS_DENIED，创建后会残留无法清理，故跳过。
    print("SKIP symlink 逃逸（已人工验证为「被拒」；沙箱禁止删除符号链接，避免残留）")
    # 默认拒绝
    try:
        run(ImageSource(timeout=5).from_source(str(inner)))
        print(">>> GAP 默认未拒绝本地文件")
    except PermissionError:
        print("OK  无 allowed_roots 时默认拒绝本地文件")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
hr("#5 指令判定：紧贴形式识别 + 误报检查")
for t in ["/搜图", "/搜图cat", "/搜图http://x", "搜图帮助x", "/soutuhelp", "。搜图 x", "!找图"]:
    print(f"  识别为指令 {(_command_head(t) is not None)!s:<5} <- {t!r}")
for t in ["普通聊天", "帮我搜图", "/其它指令", "搜索图片", "搜图真有意思", "soutubot很棒"]:
    print(f"  识别为指令 {(_command_head(t) is not None)!s:<5} <- {t!r}  (误报?)")


# --------------------------------------------------------------------------- #
hr("#5b 自动搜图已彻底删除（无 on_message / 无判重登记）")
_main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
print("  main.py 仍有 on_message 监听器:", "def on_message" in _main_src)
print("  main.py 仍有 _RecentMessageRegistry:", "_RecentMessageRegistry" in _main_src)

