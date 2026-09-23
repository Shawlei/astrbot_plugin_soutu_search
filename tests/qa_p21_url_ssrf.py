"""QA 独立复验 P2-1：图片直链入口的 SSRF 攻击 + 关键词回归 + 自动判别 + 成功/失败路径。

自 0.5.0 起「图片直链 / 图片」入口分挂在 **搜本** 与 **搜图** 上：

- ``搜本 <图片链接>`` → 下载后走 soutubot 以图搜本子；
- ``搜图 <图片链接>`` / ``搜图`` + 图片 → 下载后走 SauceNAO 反查（未配置 key 时只回引导、不下载）；
- ``搜图 <关键词>`` → 走 Safebooru 关键词搜图。

本轮断言：「内网/协议限制依然生效」，且「搜图未配置 key 时图片分支零下载」。

运行:
    python tests/qa_p21_url_ssrf.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot.api.message_components import Image  # noqa: E402  (mocked)

from astrbot_plugin_soutu_search.core.formatter import SearchResult, SourceOutcome  # noqa: E402
from astrbot_plugin_soutu_search.core.image_source import ImagePayload, ImageSource  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    SoutuSearchPlugin,
    BOOK_KEYWORD_NOT_SUPPORTED_TEXT,
    HELP_TEXT,
    IMAGE_URL_FETCH_FAIL_TEXT,
    SAUCENAO_KEY_MISSING_TEXT,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [x async for x in agen]


class FakeMessageObj:
    def __init__(self, with_image=False):
        self.message_id = "m1"
        # 无 message 属性 -> from_event 取不到图片
        if with_image:
            self.message = [Image(url="http://img.example/q.jpg")]


class FakeEvent:
    def __init__(self, text="", with_image=False):
        self.unified_msg_origin = "umo-A"
        self.message_str = text
        self.message_obj = FakeMessageObj(with_image)

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
        return SourceOutcome(results=[SearchResult(title="t", source="s", url="u", score=90.0)])

    async def close(self):
        pass


class _StubBooru:
    def __init__(self):
        self.calls = []

    async def search_by_tags(self, tags, **kw):
        self.calls.append(tags)
        return SourceOutcome(results=[SearchResult(title="t", source="s", url="u", score=90.0)])

    async def close(self):
        pass


class _StubSaucenao:
    def __init__(self):
        self.calls = []

    async def search(self, image, **kw):
        self.calls.append(kw)
        return SourceOutcome(results=[SearchResult(title="t", source="s", url="u", score=90.0)])

    async def close(self):
        pass


def make_plugin(**cfg):
    p = SoutuSearchPlugin(object(), cfg)
    # 用「无 allowed_roots」的真实 ImageSource，纯文本事件 -> 走 from_source 直链分支
    p.image_source = ImageSource(timeout=8)
    return p


def hr(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


# --------------------------------------------------------------------------- #
hr("A. SSRF 攻击：把内网/协议 payload 当『文本参数』传给 搜本 / 搜图")
# --------------------------------------------------------------------------- #
ATTACKS = [
    "http://127.0.0.1/x.jpg",
    "http://2130706433/",
    "http://0x7f000001/x.jpg",
    "http://127.0.0.1.nip.io/",
    "http://localhost/x.jpg",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/x.jpg",
    "http://192.168.1.1/x.jpg",
    "http://[::1]/x.jpg",
    "http://0.0.0.0/x.jpg",
    "file:///etc/passwd",
    "http://127.1/x.jpg",
    "http://0177.0.0.1/x.jpg",
]

print("--- 搜本（book_cmd）---")
for payload in ATTACKS:
    p = make_plugin()
    stub_soutu = _StubSoutu()
    p.soutu = stub_soutu
    out = run(collect(p.book_cmd(FakeEvent(payload), args=payload)))
    kind = out[0][0] if out else "?"
    text = out[0][1] if out and kind == "plain" else ""
    # 判定
    if stub_soutu.calls:
        verdict = ">>> FETCHED-AND-SEARCHED (可能绕过!)"
    elif kind == "plain" and ("拒绝访问内网" in text or "不支持的图片来源协议" in text):
        verdict = "BLOCKED(SSRF拒绝)"
    elif kind == "plain" and "无法获取图片链接" in text:
        verdict = f"BLOCKED/其他错误 {text[:40]!r}"
    elif kind == "plain" and text == BOOK_KEYWORD_NOT_SUPPORTED_TEXT.format(p="/"):
        verdict = "非 http 直链 -> 按『搜本不支持关键词』引导（未走直链）"
    else:
        verdict = f"other kind={kind}"
    print(f"  搜本 {payload:<42} -> {verdict}")

print("\n--- 搜图（sou_cmd, 已配置 key）---")
for payload in ATTACKS:
    p = make_plugin(saucenao_api_key="k")
    stub_sa = _StubSaucenao()
    stub_booru = _StubBooru()
    p.saucenao = stub_sa
    p.booru = stub_booru
    out = run(collect(p.sou_cmd(FakeEvent(payload), args=payload)))
    kind = out[0][0] if out else "?"
    text = out[0][1] if out and kind == "plain" else ""
    if kind == "plain" and "拒绝访问内网" in text:
        verdict = "BLOCKED(SSRF拒绝)"
    elif kind == "plain" and "无法获取图片链接" in text:
        verdict = "BLOCKED/其他错误 " + text[:40]
    elif stub_sa.calls:
        verdict = ">>> FETCHED-AND-SEARCHED (可能绕过!)"
    elif stub_booru.calls:
        verdict = "非 http 直链 -> 按关键词走 Safebooru（未走直链）"
    else:
        verdict = f"other kind={kind}"
    print(f"  搜图 {payload:<40} -> {verdict}")


# --------------------------------------------------------------------------- #
hr("B. 直接对真实 ImageSource.from_source 打 SSRF（看异常类型）")
# --------------------------------------------------------------------------- #
src = ImageSource(timeout=8)
for payload in ATTACKS:
    try:
        run(src.from_source(payload))
        print(f"  >>> {payload!r}: 未被拒绝（返回了 payload）")
    except Exception as e:
        print(f"  {payload!r:<42} -> {type(e).__name__}: {str(e)[:70]}")


# --------------------------------------------------------------------------- #
hr("C. 关键词路径：搜图 非 URL 文本仍是关键词搜图")
# --------------------------------------------------------------------------- #
for kw in ["猫娘 白丝", "cat_ears", "初音未来", "httpfoo"]:
    p = make_plugin()
    stub_booru = _StubBooru()
    stub_soutu = _StubSoutu()
    stub_sa = _StubSaucenao()
    p.booru = stub_booru
    p.soutu = stub_soutu
    p.saucenao = stub_sa
    out = run(collect(p.sou_cmd(FakeEvent(kw), args=kw)))
    tag = "关键词" if stub_booru.calls else ("以图" if stub_soutu.calls else "?")
    print(f"  搜图 {kw!r:<14} -> 走了 {tag} "
          f"(booru={stub_booru.calls}, soutu={len(stub_soutu.calls)}, saucenao={len(stub_sa.calls)})")


# --------------------------------------------------------------------------- #
hr("D. 成功路径：mock from_source 返回图片 -> 必须真的发起反查/以图搜图")
# --------------------------------------------------------------------------- #
p = make_plugin()
calls = []


async def fake_from_source(url):
    calls.append(url)
    return ImagePayload(data=PNG, mime="image/png", filename="a.png")


p.image_source.from_source = fake_from_source
stub = _StubSoutu()
p.soutu = stub
out = run(collect(p.book_cmd(FakeEvent("http://example.com/a.jpg"), args="http://example.com/a.jpg")))
print(f"  搜本 成功路径: from_source calls={calls}, soutu.search 次数={len(stub.calls)}, 首块={out[0][0]}")

p2 = make_plugin(saucenao_api_key="k")
calls2 = []


async def fake_from_source2(url):
    calls2.append(url)
    return ImagePayload(data=PNG, mime="image/png", filename="a.png")


p2.image_source.from_source = fake_from_source2
stub2 = _StubSaucenao()
p2.saucenao = stub2
out2 = run(collect(p2.sou_cmd(FakeEvent("http://example.com/a.jpg"), args="http://example.com/a.jpg")))
print(f"  搜图 成功路径: from_source calls={calls2}, saucenao.search 次数={len(stub2.calls)}, 首块={out2[0][0]}")


# --------------------------------------------------------------------------- #
hr("E. 失败路径：from_source 抛错 -> 回 IMAGE_URL_FETCH_FAIL_TEXT 且不崩")
# --------------------------------------------------------------------------- #
def boom_plugin(**cfg):
    p = make_plugin(**cfg)

    async def boom(url):
        raise RuntimeError("模拟取图失败")

    p.image_source.from_source = boom
    return p


o = run(collect(boom_plugin().book_cmd(FakeEvent("http://x/a.jpg"), args="http://x/a.jpg")))
print(f"  搜本 失败: kind={o[0][0]} 含提示={'无法获取图片链接' in o[0][1]}")
print("     text:", o[0][1].replace("\n", " / ")[:90])

o2 = run(collect(boom_plugin(saucenao_api_key="k").sou_cmd(
    FakeEvent("http://x/a.jpg"), args="http://x/a.jpg")))
print(f"  搜图 失败: kind={o2[0][0]} 含提示={'无法获取图片链接' in o2[0][1]}")
print("     text:", o2[0][1].replace("\n", " / ")[:90])


# --------------------------------------------------------------------------- #
hr("F. 【0.6.0】搜图自动判别：图片→双源反查(SauceNAO+ascii2d)；关键词→Safebooru")
# --------------------------------------------------------------------------- #

# F1. 有 key + 消息带图片 -> SauceNAO，且不碰 booru/soutu
p = make_plugin(saucenao_api_key="k")
stub_sa = _StubSaucenao()
stub_booru = _StubBooru()
stub_soutu = _StubSoutu()
p.saucenao, p.booru, p.soutu = stub_sa, stub_booru, stub_soutu
dl = []


async def fe_img(_ev):
    dl.append("<from_event>")
    return ImagePayload(data=PNG, mime="image/png", filename="a.png")


p.image_source.from_event = fe_img
out = run(collect(p.sou_cmd(FakeEvent("/搜图", with_image=True), "")))
ok = len(stub_sa.calls) == 1 and not stub_booru.calls and not stub_soutu.calls
print(f"  {'OK ' if ok else '>>> GAP'}  搜图+图片(有key) -> saucenao={len(stub_sa.calls)} "
      f"booru={stub_booru.calls} soutu={len(stub_soutu.calls)} 下载={dl} 首块={out[0][0]}")

# F2. 有 key + 图片链接 -> 下载后 SauceNAO
p2 = make_plugin(saucenao_api_key="k")
stub_sa2 = _StubSaucenao()
stub_booru2 = _StubBooru()
p2.saucenao, p2.booru = stub_sa2, stub_booru2
dl2 = []


async def fs2(url):
    dl2.append(url)
    return ImagePayload(data=PNG, mime="image/png", filename="a.png")


p2.image_source.from_source = fs2
out2 = run(collect(p2.sou_cmd(FakeEvent("/搜图 http://e.com/a.jpg"), "http://e.com/a.jpg")))
ok2 = dl2 == ["http://e.com/a.jpg"] and len(stub_sa2.calls) == 1 and not stub_booru2.calls
print(f"  {'OK ' if ok2 else '>>> GAP'}  搜图+图片链接(有key) -> 下载={dl2} "
      f"saucenao={len(stub_sa2.calls)} booru={stub_booru2.calls} 首块={out2[0][0]}")

# F3. 关键词 -> Safebooru，不碰 saucenao/soutu
p3 = make_plugin()
stub_sa3 = _StubSaucenao()
stub_booru3 = _StubBooru()
stub_soutu3 = _StubSoutu()
p3.saucenao, p3.booru, p3.soutu = stub_sa3, stub_booru3, stub_soutu3
out3 = run(collect(p3.sou_cmd(FakeEvent("/搜图 cat_ears"), "cat_ears")))
ok3 = stub_booru3.calls == ["cat_ears"] and not stub_sa3.calls and not stub_soutu3.calls
print(f"  {'OK ' if ok3 else '>>> GAP'}  搜图+关键词 -> booru={stub_booru3.calls} "
      f"saucenao={len(stub_sa3.calls)} soutu={len(stub_soutu3.calls)} 首块={out3[0][0]}")

# F4. 无 key + 图片 -> 跳过 SauceNAO，但运行 ascii2d（0.6.0 起双源并行，ascii2d 免 key）
p4 = make_plugin()  # 无 key
dl4 = []


async def fe_img4(_ev):
    dl4.append("<from_event>")
    return ImagePayload(data=PNG, mime="image/png", filename="a.png")


async def fs4(url):
    dl4.append(url)
    return ImagePayload(data=PNG, mime="image/png", filename="a.png")


p4.image_source.from_event = fe_img4
p4.image_source.from_source = fs4
out4 = run(collect(p4.sou_cmd(FakeEvent("/搜图", with_image=True), "")))
text4 = "\n".join(getattr(c, "text", "") for c in out4[0][1]) if out4[0][0] == "chain" else out4[0][1]
ok4 = SAUCENAO_KEY_MISSING_TEXT in text4 and len(dl4) == 1
print(f"  {'OK ' if ok4 else '>>> GAP'}  搜图+图片(无key) -> 跳过SauceNAO引导={SAUCENAO_KEY_MISSING_TEXT in text4} 下载={dl4}")

# F5. 无 key + 关键词 -> 仍可关键词搜图
p5 = make_plugin()  # 无 key
stub_booru5 = _StubBooru()
p5.booru = stub_booru5
out5 = run(collect(p5.sou_cmd(FakeEvent("/搜图 猫娘"), "猫娘")))
ok5 = stub_booru5.calls == ["猫娘"] and out5[0][0] == "chain"
print(f"  {'OK ' if ok5 else '>>> GAP'}  搜图+关键词(无key) -> booru={stub_booru5.calls} 首块={out5[0][0]}")

print("\n完成。")
