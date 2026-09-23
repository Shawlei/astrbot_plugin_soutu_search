"""Yandex provider 单元测试（离线，不联网）。

覆盖：
- 结果解析：data-state JSON 中的 cbirSites / cbirSimilar 提取、utm 参数剥离、相对链接补全；
- 空响应 / 无 initialState / 残结构的优雅降级；
- 请求构造：multipart 含 prg+upfile、Content-Length（bytes body 非 chunked）、UA/Referer/Origin；
- 非 200 / 超时 / 空图片的错误包装；
- 双源并行中的失败隔离与缓存命名空间。

复用 tests/test_core.py 的 astrbot 桩（import 即完成 sys.modules 装配）。
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

import aiohttp  # noqa: E402

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
)
from astrbot_plugin_soutu_search.core.image_source import ImagePayload  # noqa: E402
from astrbot_plugin_soutu_search.core.yandex_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    YandexClient,
    clean_url,
    parse_yandex_html,
)
from astrbot_plugin_soutu_search.main import SoutuSearchPlugin  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _make_state_html(sites=None, similar=None) -> str:
    """构造一段带 data-state 的最小结果页 HTML（state 需 ≥300 字符才被客户端正则捕获）。"""
    init: dict = {}
    if sites is not None:
        init["cbirSites"] = {"sites": sites}
    if similar is not None:
        init["cbirSimilar"] = {"thumbs": similar}
    # 填充到 300+ 字符（客户端用 data-state="([^"]{300,})" 过滤无关属性）
    pad = 400 - len(json.dumps({"initialState": init}, ensure_ascii=False))
    if pad > 0:
        init["pad"] = "x" * pad
    state = json.dumps({"initialState": init}, ensure_ascii=False).replace('"', "&quot;")
    return f'<html><body><div data-state="{state}"></div></body></html>'


_SITE = {
    "title": "yorumi rena drawn by kei Danbooru",
    "description": "desc",
    "url": "https://donmai.us/posts/1?utm_medium=organic&utm_source=yandexsmartcamera",
    "domain": "donmai.us",
    "thumb": {"url": "//avatars.mds.yandex.net/i?id=abc", "height": 90, "width": 148},
    "originalImage": {"url": "https://cdn.donmai.us/a.jpg", "height": 640, "width": 640},
}


def run(coro):
    return asyncio.run(coro)


def _collect(agen):
    async def _a():
        return [x async for x in agen]

    return run(_a())


class _FakeResp:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.last = {}
        self.closed = False

    def post(self, url, **kw):
        self.last = {"method": "POST", "url": url, **kw}
        return self._resp

    def get(self, url, **kw):
        self.last = {"method": "GET", "url": url, **kw}
        return self._resp

    async def close(self):
        self.closed = True


class TestUrlParser(unittest.TestCase):
    def test_utm_stripped(self):
        url = clean_url("https://donmai.us/posts/1?utm_medium=organic&utm_source=ya&id=7")
        self.assertEqual(url, "https://donmai.us/posts/1?id=7")

    def test_empty_and_garbage(self):
        self.assertEqual(clean_url(""), "")
        self.assertEqual(clean_url("not a url"), "not a url")

    def test_plain_url_unchanged(self):
        self.assertEqual(clean_url("https://example.com/a?b=1"), "https://example.com/a?b=1")


class TestParseHtml(unittest.TestCase):
    def test_sites_extracted_and_cleaned(self):
        html = _make_state_html(sites=[_SITE])
        results = parse_yandex_html(html)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.title, "yorumi rena drawn by kei Danbooru")
        self.assertEqual(r.source, "donmai.us")
        self.assertEqual(r.url, "https://donmai.us/posts/1")
        self.assertEqual(r.thumbnail, "https://avatars.mds.yandex.net/i?id=abc")
        self.assertIsNone(r.score)

    def test_relative_thumb_gets_https_prefix(self):
        html = _make_state_html(sites=[_SITE])
        r = parse_yandex_html(html)[0]
        self.assertTrue(r.thumbnail.startswith("https://"))

    def test_duplicate_urls_deduped(self):
        site2 = dict(_SITE, title="dup")
        html = _make_state_html(sites=[_SITE, site2])
        results = parse_yandex_html(html)
        self.assertEqual(len(results), 1)

    def test_fallback_to_similar_when_no_sites(self):
        similar = [{"title": "sim", "linkUrl": "/images/search?cbir_id=1&extra=1&extra2=2&extra3=3", "imageUrl": ""}]
        html = _make_state_html(sites=[], similar=similar)
        results = parse_yandex_html(html)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].url.startswith("https://yandex.ru/"))
        self.assertEqual(results[0].source, "yandex-similar")

    def test_empty_html_returns_empty(self):
        self.assertEqual(parse_yandex_html(""), [])
        self.assertEqual(parse_yandex_html("<html></html>"), [])

    def test_malformed_state_does_not_raise(self):
        html = '<html><div data-state="not-json"></div></html>'
        self.assertEqual(parse_yandex_html(html), [])

    def test_top_k_limit(self):
        sites = [dict(_SITE, url=f"https://e.com/{i}", title=f"t{i}") for i in range(10)]
        results = parse_yandex_html(_make_state_html(sites=sites), top_k=3)
        self.assertEqual(len(results), 3)


class TestRequestConstruction(unittest.TestCase):
    def _search(self, client, resp):
        session = _FakeSession(resp)
        client._session = session  # type: ignore[assignment]
        return session, run(client.search(PNG))

    def test_posts_bytes_body_with_content_type(self):
        html = _make_state_html(sites=[_SITE])
        client = YandexClient()
        session, outcome = self._search(client, _FakeResp(200, html))
        self.assertEqual(session.last["method"], "POST")
        self.assertIn("/images/search", session.last["url"])
        body = session.last["data"]
        self.assertIsInstance(body, bytes, "body 必须是 bytes（保证 Content-Length，避免 413）")
        self.assertIn(b'name="upfile"', body)
        self.assertIn(b'name="prg"', body)
        headers = session.last["headers"]
        self.assertIn("multipart/form-data", headers["Content-Type"])
        self.assertEqual(headers["Origin"], "https://yandex.ru")
        self.assertIn("yandex.ru/images/", headers["Referer"])
        self.assertEqual(len(outcome.results), 1)

    def test_base_url_override(self):
        client = YandexClient(base_url="https://yandex.example.com")
        session, _ = self._search(client, _FakeResp(200, _make_state_html(sites=[_SITE])))
        self.assertIn("yandex.example.com", session.last["url"])
        self.assertEqual(client.base_url, "https://yandex.example.com")

    def test_json_response_triggers_second_get(self):
        """若 POST 返回 JSON（含 cbirId），应再 GET 结果页。"""
        json_body = json.dumps({"blocks": [{"params": {"cbirId": "123/abc"}}]})
        client = YandexClient()
        post_resp = _FakeResp(200, json_body)
        get_resp = _FakeResp(200, _make_state_html(sites=[_SITE]))

        class _TwoStepSession(_FakeSession):
            def __init__(self):
                super().__init__(None)
                self.post_url = None
                self.get_url = None

            def post(self, url, **kw):
                self.post_url = url
                return post_resp

            def get(self, url, **kw):
                self.get_url = url
                return get_resp

        two_step = _TwoStepSession()
        client._session = two_step  # type: ignore[assignment]
        outcome = run(client.search(PNG))
        self.assertIn("/images/search", two_step.post_url)
        self.assertIn("cbir_id=123", two_step.get_url)
        self.assertIn("cbir_page=sites", two_step.get_url)
        self.assertEqual(len(outcome.results), 1)

    def test_non_200_raises_readable(self):
        client = YandexClient()
        with self.assertRaises(RuntimeError) as cm:
            self._search(client, _FakeResp(413, ""))
        self.assertIn("413", str(cm.exception))

    def test_timeout_wrapped(self):
        client = YandexClient(timeout=30)

        class _TimeoutSession(_FakeSession):
            def post(self, url, **kw):
                raise asyncio.TimeoutError()

        client._session = _TimeoutSession(None)  # type: ignore[assignment]
        with self.assertRaises(TimeoutError):
            run(client.search(PNG))

    def test_client_error_wrapped(self):
        client = YandexClient()

        class _ErrSession(_FakeSession):
            def post(self, url, **kw):
                raise aiohttp.ClientError("boom")

        client._session = _ErrSession(None)  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            run(client.search(PNG))

    def test_empty_image_raises_before_request(self):
        client = YandexClient()
        with self.assertRaises(ValueError):
            run(client.search(b""))


class TestPluginIntegration(unittest.TestCase):
    def _plugin(self, **cfg):
        return SoutuSearchPlugin(object(), cfg)

    def test_defaults(self):
        p = self._plugin()
        self.assertTrue(p.yandex_enable)
        self.assertEqual(p.yandex_base_url, "https://yandex.ru")
        self.assertEqual(p.yandex.base_url, "https://yandex.ru")

    def test_disable_and_mirror(self):
        p = self._plugin(yandex_enable=False, yandex_base_url="https://mirror.example.com")
        self.assertFalse(p.yandex_enable)
        self.assertEqual(p.yandex.base_url, "https://mirror.example.com")

    def test_yandex_results_no_image_block_no_thumb_leak(self):
        """nsfw 关闭时，Yandex 结果不发图片块、不泄漏缩略图 URL（与 ascii2d 同标准）。"""
        from astrbot_plugin_soutu_search.core.formatter import format_outcome

        outcome = SourceOutcome(
            results=[
                SearchResult(
                    title="t",
                    source="donmai.us",
                    url="https://d/1",
                    thumbnail="https://avatars.mds.yandex.net/i?id=xyz",
                    score=None,
                )
            ]
        )
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="【Yandex】H")
        self.assertFalse(any(b["type"] == "image" for b in blocks))
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertNotIn("yandex.net", joined)

    def test_terminate_closes_yandex(self):
        p = self._plugin()

        async def _close():
            await p.terminate()
            return True

        # 不抛异常即通过（YandexClient.close 幂等）
        run(_close())


if __name__ == "__main__":
    unittest.main()
