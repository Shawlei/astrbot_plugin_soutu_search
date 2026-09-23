"""QA 修复回归测试：P2-1（图片直链）、P2-2（dbmask=0）、P3（warning 去重 emoji）。

- P2-1：帮助文案宣称支持 `<指令> <图片链接>`，故 `搜本` / `搜P站` 必须**真正**下载该链接；
  失败时给出明确提示（而非死循环回同一句用法提示）。自 0.4.0 起「搜图」拆为纯关键词指令，
  收到图片链接只回引导提示、**不下载**。
- P2-2：`dbmask=0`（意图"不限库"）时**不发送** `dbmask` 参数（避免误发 0 导致搜不到）。
- P3：provider 自带的 `⚠️` 前缀 warning 与 formatter 叠加时不得出现「⚠️ ⚠️」。

运行::
    python -m unittest tests.test_saucenao_dispatch_fixes -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.image_source import ImagePayload  # noqa: E402
from astrbot_plugin_soutu_search.core.saucenao_client import SaucenaoClient  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    IMAGE_URL_FETCH_FAIL_TEXT,
    SAUCENAO_NO_IMAGE_TEXT,
    SoutuSearchPlugin,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [x async for x in agen]


def collect(agen):
    return run(_collect(agen))


# --------------------------------------------------------------------------- #
# 假 session（用于 SaucenaoClient 请求参数断言）
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, status=200, body="", raise_exc=None):
        self.status = status
        self._body = body
        self._raise = raise_exc

    async def text(self):
        return self._body

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
        self.last = None

    def post(self, url, **kw):
        self.last = {"url": url, **kw}
        return self._resp


class FakeEvent:
    def __init__(self, umo="umo-A", text=""):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = type("M", (), {"message_id": "m1"})()

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, comps):
        return ("chain", comps)


def _outcome(title="作品"):
    return SourceOutcome(
        results=[SearchResult(title=title, source="来自 pixiv 库", url="https://x/y", score=95.0)],
        warnings=[],
        meta={},
    )


class _StubSaucenao:
    def __init__(self):
        self.calls = []

    async def search(self, image, **kw):
        self.calls.append((image, kw))
        return _outcome()

    async def close(self):
        pass


class _StubSoutu:
    def __init__(self):
        self.calls = []

    async def search(self, image, **kw):
        self.calls.append(kw)
        return _outcome()

    async def close(self):
        pass


class _StubBooru:
    def __init__(self):
        self.calls = []

    async def search_by_tags(self, tags, **kw):
        self.calls.append(tags)
        return _outcome()

    async def close(self):
        pass


# ===========================================================================
# P2-1：图片直链真正被处理
# ===========================================================================
class TestImageUrlDispatch(unittest.TestCase):
    def _plugin_with_source(self, calls):
        p = SoutuSearchPlugin(object(), {"saucenao_api_key": "k"})

        async def fake_from_source(src):
            calls.append(src)
            return ImagePayload(data=PNG, mime="image/png", filename="a.png")

        p.image_source.from_source = fake_from_source  # type: ignore[assignment]
        return p

    def test_pixiv_url_is_downloaded_then_searched(self):
        calls = []
        p = self._plugin_with_source(calls)
        stub = _StubSaucenao()
        p.saucenao = stub  # type: ignore[assignment]
        ev = FakeEvent(text="/搜P站 http://x/a.jpg")
        out = collect(p.pixiv_cmd(ev, args="http://x/a.jpg"))
        self.assertEqual(calls, ["http://x/a.jpg"], "应通过 from_source 下载该链接")
        self.assertEqual(len(stub.calls), 1, "下载成功后应发起一次反查")
        self.assertEqual(out[0][0], "chain", "有结果应返回消息链，而非用法提示")

    def test_pixiv_url_fetch_failure_gives_readable_message(self):
        p = SoutuSearchPlugin(object(), {"saucenao_api_key": "k"})

        async def boom(src):
            raise RuntimeError("出于安全考虑，拒绝访问内网/保留地址")

        p.image_source.from_source = boom  # type: ignore[assignment]
        out = collect(p.pixiv_cmd(FakeEvent(), args="http://10.0.0.1/a.jpg"))
        self.assertEqual(out[0][0], "plain")
        self.assertIn("无法获取图片链接", out[0][1])
        self.assertNotEqual(out[0][1], SAUCENAO_NO_IMAGE_TEXT.format(p="/"))

    def test_pixiv_no_image_no_url_still_usage_hint(self):
        p = SoutuSearchPlugin(object(), {"saucenao_api_key": "k"})
        out = collect(p.pixiv_cmd(FakeEvent(), args=""))
        self.assertEqual(out[0][1], SAUCENAO_NO_IMAGE_TEXT.format(p="/"))

    def test_soutu_url_is_downloaded_then_image_searched(self):
        """图片直链下载后走以图搜图 —— 自 0.4.0 起该路径挂在「搜本」（soutubot）上。"""
        calls = []
        p = SoutuSearchPlugin(object(), {})

        async def fake_from_source(src):
            calls.append(src)
            return ImagePayload(data=PNG, mime="image/png", filename="a.png")

        p.image_source.from_source = fake_from_source  # type: ignore[assignment]
        stub = _StubSoutu()
        p.soutu = stub  # type: ignore[assignment]
        ev = FakeEvent(text="/搜本 http://x/a.jpg")
        out = collect(p.book_cmd(ev, args="http://x/a.jpg"))
        self.assertEqual(calls, ["http://x/a.jpg"])
        self.assertEqual(len(stub.calls), 1, "图片链接应走以图搜图，而非关键词搜图")
        self.assertEqual(out[0][0], "chain")

    def test_soutu_url_fetch_failure_readable(self):
        """「搜本」直链下载失败 → 明确提示（不静默失败）。"""
        p = SoutuSearchPlugin(object(), {})

        async def boom(src):
            raise RuntimeError("SSRF 拒绝")

        p.image_source.from_source = boom  # type: ignore[assignment]
        out = collect(p.book_cmd(FakeEvent(), args="http://10.0.0.1/a.jpg"))
        self.assertEqual(out[0][0], "plain")
        self.assertIn("无法获取图片链接", out[0][1])

    def test_soutu_keyword_still_keyword_search(self):
        p = SoutuSearchPlugin(object(), {})
        stub = _StubBooru()
        p.booru = stub  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 cat"), args="cat"))
        self.assertEqual(stub.calls, ["cat"], "非 URL 文本仍走关键词搜图")
        self.assertEqual(out[0][0], "chain")

    def test_search_url_is_not_downloaded_but_hinted(self):
        """「搜图」收到图片直链 → 回引导提示，**不下载**（省带宽 + 收敛 SSRF 面）。"""
        calls = []
        p = SoutuSearchPlugin(object(), {})

        async def fake_from_source(src):
            calls.append(src)
            return ImagePayload(data=PNG, mime="image/png", filename="a.png")

        p.image_source.from_source = fake_from_source  # type: ignore[assignment]
        stub_soutu = _StubSoutu()
        stub_booru = _StubBooru()
        p.soutu = stub_soutu  # type: ignore[assignment]
        p.booru = stub_booru  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent(text="/搜图 http://x/a.jpg"), args="http://x/a.jpg"))
        self.assertEqual(calls, [], "「搜图」不得下载图片链接")
        self.assertEqual(stub_soutu.calls, [], "「搜图」不得走以图搜图")
        self.assertEqual(stub_booru.calls, [], "「搜图」不得把 URL 当关键词搜")
        self.assertEqual(out[0][0], "plain")
        self.assertIn("不接受图片", out[0][1])

    def test_help_text_still_no_double_emoji_claim(self):
        # 帮助文案仍宣称支持图片链接（现在确已实现）
        p = SoutuSearchPlugin(object(), {})
        self.assertIn("搜P站 <图片链接>", p._saucenao_help_text())


# ===========================================================================
# P2-2：dbmask=0 不发送该参数
# ===========================================================================
class TestDbMaskParam(unittest.TestCase):
    def _search(self, client):
        client._session = FakeSession(FakeResp(200, json.dumps({"header": {"status": 0}, "results": []})))
        run(client.search(b"\xff\xd8\xffIMG"))
        return client._session.last["params"]

    def test_mask_zero_omits_dbmask(self):
        params = self._search(SaucenaoClient(api_key="K", db_mask=0))
        self.assertNotIn("dbmask", params, "dbmask=0（不限库）时不应发送该参数")

    def test_mask_96_sends_dbmask(self):
        params = self._search(SaucenaoClient(api_key="K", db_mask=96))
        self.assertEqual(params["dbmask"], "96")

    def test_mask_512_sends_dbmask(self):
        params = self._search(SaucenaoClient(api_key="K", db_mask=512))
        self.assertEqual(params["dbmask"], "512")

    def test_other_params_still_present_when_mask_zero(self):
        params = self._search(SaucenaoClient(api_key="K", db_mask=0))
        self.assertEqual(params["output_type"], "2")
        self.assertIn("numres", params)
        self.assertIn("minsim", params)
        self.assertIn("hide", params)


# ===========================================================================
# P3：warning 前缀去重（不出现 ⚠️ ⚠️）
# ===========================================================================
class TestWarningDedup(unittest.TestCase):
    def _text(self, warnings, results=None):
        outcome = SourceOutcome(
            results=results if results is not None else [],
            warnings=warnings,
        )
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="H")
        return "\n".join(b.get("text", "") for b in blocks)

    def test_prefixed_warning_not_doubled(self):
        text = self._text(["⚠️ SauceNAO 今日配额已用完（免费账户 150 次/天），请明天再试。"])
        self.assertIn("⚠️ SauceNAO 今日配额已用完", text)
        self.assertNotIn("⚠️ ⚠️", text)

    def test_plain_warning_gets_single_prefix(self):
        text = self._text(["站点返回部分结果（partial）。"])
        self.assertIn("⚠️ 站点返回部分结果", text)
        self.assertNotIn("⚠️ ⚠️", text)

    def test_mixed_warnings_no_double(self):
        text = self._text(
            ["⚠️ 今日配额已用完", "普通警告"],
            results=[SearchResult(title="t", source="s", url="u", score=90.0)],
        )
        self.assertNotIn("⚠️ ⚠️", text)
        self.assertIn("⚠️ 今日配额已用完", text)
        self.assertIn("⚠️ 普通警告", text)

    def test_blank_warning_skipped(self):
        text = self._text(["", "   "])
        self.assertNotIn("⚠️", text)

    def test_quota_from_provider_not_double(self):
        # 真实 provider warning 走一遍 formatter
        p = {
            "header": {"status": 0, "long_remaining": 0, "short_remaining": 0},
            "results": [],
        }
        from astrbot_plugin_soutu_search.core.saucenao_client import parse_saucenao_response

        outcome = parse_saucenao_response(p)
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="H")
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertIn("配额", text)
        self.assertNotIn("⚠️ ⚠️", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
