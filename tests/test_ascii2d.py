"""ascii2d provider（`搜图` 的图片反查分支 · 双源之一）单元测试。

覆盖（依据 ``recon/API_ASCII2D.md`` 规范，离线验证；本机 curl 不可达 ascii2d.net）：
- 请求构造：POST 路径 ``/search/file``、multipart 字段名 ``file``、base_url 生效、请求头；
- ``bovw``：二次 GET（/color/ -> /bovw/）；结果页 URL 不含 ``/color/`` 时**优雅降级**为解析首次响应；
- 网络错误包装：超时 / 传输错误 / 非 200 / 空响应 → 可读 ``RuntimeError``（不静默返回空）；
- HTML 解析各分支：正常 / 空 / 无 item-box / 残结构 / 相对 URL / 来源标记大小写 / 标题回退 / 2ch 噪声；
- **``score=None`` 不被 min_similarity 过滤**（ascii2d 无相似度）；
- 统一结果模型字段映射（含 ``extra`` 的 detail/hash/author/author_url/source_mark）；
- 双源并行：**单源失败绝不影响另一源**；两源都失败给明确提示；两源都关给配置提示；
- api_key 缺失时仍跑 ascii2d；
- 新配置 4 项与容错（非法类型回退）；
- NSFW 关闭时 ascii2d 结果**无 image 块、不泄漏缩略图 URL**；
- ascii2d 缓存命名空间独立（不与 soutu/saucenao 互相污染）。

复用 tests/test_core.py 的 astrbot 桩（import 即完成 sys.modules 装配）。
运行::
    python -m unittest tests.test_ascii2d -v
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

from astrbot_plugin_soutu_search.core import cache as cache_mod  # noqa: E402
from astrbot_plugin_soutu_search.core.ascii2d_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    SOURCE_DISPLAY_NAMES,
    Ascii2dClient,
    build_result_from_item,
    parse_ascii2d_html,
    parse_ascii2d_response,
)
from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.image_source import ImagePayload  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    ALL_SOURCES_DISABLED_TEXT,
    SoutuSearchPlugin,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# 一段贴近真实的 ascii2d 结果页（两个 item-box：pixiv + twitter）
SAMPLE_HTML = """
<html><body>
<div class="row result">
  <div class="row item-box">
    <div class="col-md-2"><img src="/thumb/pixiv_hash.jpg" class="preview"></div>
    <div class="col-md-10">
      <div class="col-md-10 detail-box gray-link">
        <h6><a href="https://www.pixiv.net/artworks/12345">作品标题A</a></h6>
        <small>pixiv</small>
        <small>1280x720 JPEG 154.3KB</small>
        <a href="https://www.pixiv.net/users/999">画师A</a>
        <div class="hash">hashA</div>
      </div>
    </div>
  </div>
  <div class="row item-box">
    <img src="/thumb/twitter_hash.jpg">
    <div class="detail-box gray-link">
      <small>Twitter</small>
      <a href="//twitter.com/someone/status/1">tweet title</a>
      <a href="/user/bob">bob</a>
      <small>800x600 PNG 90KB</small>
      <div class="hash">hashB</div>
    </div>
  </div>
</div>
</body></html>
"""


def run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [x async for x in agen]


def collect(agen):
    return run(_collect(agen))


# --------------------------------------------------------------------------- #
# 假 session（POST/GET）
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, body="", url="", exc=None):
        self.status = status
        self._body = body
        self.url = url
        self._exc = exc

    async def text(self):
        return self._body

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, post_resp=None, get_resp=None):
        self.closed = False
        self.calls = []
        self._post = post_resp
        self._get = get_resp

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return self._post

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return self._get


def _client_with(post_resp, get_resp=None, **kw):
    client = Ascii2dClient(**kw)
    client._session = _Session(post_resp, get_resp)
    return client


def _chain_text(result) -> str:
    kind, payload = result
    if kind == "plain":
        return payload
    return "\n".join(getattr(c, "text", "") for c in payload)


class _StubAscii2d:
    def __init__(self, outcome=None, exc=None):
        self.calls = []
        self._outcome = outcome
        self._exc = exc

    async def search(self, image, **kw):
        self.calls.append(kw)
        if self._exc is not None:
            raise self._exc
        return self._outcome or SourceOutcome(
            results=[SearchResult(title="a2d", source="Pixiv", url="https://p/1", score=None)]
        )

    async def close(self):
        pass


class _StubSaucenao:
    def __init__(self, outcome=None, exc=None):
        self.calls = []
        self._outcome = outcome
        self._exc = exc

    async def search(self, image, **kw):
        self.calls.append(kw)
        if self._exc is not None:
            raise self._exc
        return self._outcome or SourceOutcome(
            results=[SearchResult(title="snao", source="来自 pixiv 库", url="u", score=95.0)]
        )

    async def close(self):
        pass


class _FakeEvent:
    def __init__(self, text="", with_image=False):
        self.unified_msg_origin = "umo-A"
        self.message_str = text
        self.message_obj = type("M", (), {"message_id": "m1"})()

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, comps):
        return ("chain", comps)


# ===========================================================================
# 1. 请求构造 / bovw / 错误包装
# ===========================================================================
def _field_names(form) -> list:
    """稳健抽取 ``aiohttp.FormData`` 的字段名（兼容多种 ``_fields`` 形态）。"""
    names = []
    for entry in getattr(form, "_fields", []):
        if isinstance(entry, dict):
            names.append(entry.get("name"))
        elif isinstance(entry, tuple):
            info = entry[0]
            if hasattr(info, "get"):
                names.append(info.get("name"))
            elif isinstance(info, (tuple, list)) and info:
                names.append(info[0])
            else:
                names.append(None)
        elif hasattr(entry, "get"):
            names.append(entry.get("name"))
        else:
            names.append(None)
    return names


class TestRequestConstruction(unittest.TestCase):
    def test_post_path_field_name_and_headers(self):
        c = _client_with(_Resp(200, SAMPLE_HTML, url="https://ascii2d.net/search/file/color/x"))
        run(c.search(b"\xff\xd8\xffIMG", filename="q.jpg", mime="image/jpeg"))
        method, url, kw = c._session.calls[0]
        self.assertEqual(method, "post")
        self.assertTrue(url.endswith("/search/file"), url)
        self.assertIn("file", _field_names(kw["data"]), "multipart 字段名必须为 file")
        headers = kw["headers"]
        self.assertEqual(headers["Referer"], "https://ascii2d.net/")
        self.assertEqual(headers["Origin"], "https://ascii2d.net")
        self.assertIn("ja", headers["Accept-Language"])
        self.assertIn("Mozilla", headers["User-Agent"])

    def test_base_url_override_and_referer(self):
        c = _client_with(_Resp(200, SAMPLE_HTML, url="https://mirror.example.com/search/file/color/x"),
                         base_url="https://mirror.example.com/")
        run(c.search(b"img"))
        _m, url, kw = c._session.calls[0]
        self.assertEqual(url, "https://mirror.example.com/search/file")
        self.assertEqual(kw["headers"]["Referer"], "https://mirror.example.com/")

    def test_bovw_second_request(self):
        c = _client_with(
            _Resp(200, SAMPLE_HTML, url="https://ascii2d.net/search/file/color/abc"),
            _Resp(200, SAMPLE_HTML, url="https://ascii2d.net/search/file/bovw/abc"),
            bovw=True,
        )
        run(c.search(b"img"))
        methods = [entry[0] for entry in c._session.calls]
        self.assertEqual(methods, ["post", "get"])
        get_url = c._session.calls[1][1]
        self.assertIn("/bovw/", get_url)
        self.assertNotIn("/color/", get_url)

    def test_bovw_degrades_when_no_color_in_final_url(self):
        """最终 URL 不含 /color/ → 不拼坏 URL、不发第二次请求，直接解析首次响应。"""
        c = _client_with(
            _Resp(200, SAMPLE_HTML, url="https://ascii2d.net/search/file/xyz"),
            bovw=True,
        )
        outcome = run(c.search(b"img"))
        self.assertEqual(len(c._session.calls), 1, "不得发出 bovw 二次请求")
        self.assertEqual(c._session.calls[0][0], "post")
        self.assertTrue(outcome.results, "降级后应仍能解析首次响应")

    def test_non_200_raises_readable(self):
        c = _client_with(_Resp(403, "<html>blocked</html>", url=""))
        with self.assertRaises(RuntimeError) as ctx:
            run(c.search(b"img"))
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_empty_body_raises(self):
        c = _client_with(_Resp(200, "   ", url="https://ascii2d.net/search/file/color/x"))
        with self.assertRaises(RuntimeError) as ctx:
            run(c.search(b"img"))
        self.assertIn("空响应", str(ctx.exception))

    def test_timeout_wrapped(self):
        c = _client_with(_Resp(exc=asyncio.TimeoutError()))
        with self.assertRaises(RuntimeError) as ctx:
            run(c.search(b"img"))
        self.assertIn("超时", str(ctx.exception))

    def test_client_error_wrapped(self):
        c = _client_with(_Resp(exc=aiohttp.ClientConnectionError("boom")))
        with self.assertRaises(RuntimeError) as ctx:
            run(c.search(b"img"))
        self.assertIn("网络错误", str(ctx.exception))

    def test_empty_image_valueerror(self):
        c = Ascii2dClient()
        with self.assertRaises(ValueError):
            run(c.search(b""))


# ===========================================================================
# 2. HTML 解析各分支
# ===========================================================================
class TestHtmlParsing(unittest.TestCase):
    def test_parse_sample_two_items(self):
        items = parse_ascii2d_html(SAMPLE_HTML)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["source_mark"], "pixiv")
        self.assertEqual(first["title"], "作品标题A")
        self.assertEqual(first["url"], "https://www.pixiv.net/artworks/12345")
        self.assertEqual(first["author"], "画师A")
        self.assertEqual(first["author_url"], "https://www.pixiv.net/users/999")
        self.assertEqual(first["detail"], "1280x720 JPEG 154.3KB")
        self.assertEqual(first["hash"], "hashA")
        self.assertEqual(first["thumbnail"], "https://ascii2d.net/thumb/pixiv_hash.jpg")

    def test_source_mark_case_insensitive_and_relative_prefix(self):
        items = parse_ascii2d_html(SAMPLE_HTML)
        second = items[1]
        self.assertEqual(second["source_mark"], "twitter", "Twitter 应大小写不敏感归一化")
        self.assertEqual(second["author"], "bob")
        # 协议相对 // → 保留协议；相对 /user/bob → 拼 ascii2d 主机
        self.assertEqual(second["url"], "https://twitter.com/someone/status/1")
        self.assertEqual(second["author_url"], "https://ascii2d.net/user/bob")

    def test_empty_and_blank(self):
        self.assertEqual(parse_ascii2d_html(""), [])
        self.assertEqual(parse_ascii2d_html("   "), [])
        self.assertEqual(parse_ascii2d_html(None), [])

    def test_no_item_box(self):
        self.assertEqual(parse_ascii2d_html("<html><body><p>no results</p></body></html>"), [])

    def test_malformed_html_does_not_raise(self):
        broken = '<div class="row item-box"><img src="/a.jpg"><div class="detail-box gray-link"><a href="/x">t'
        items = parse_ascii2d_html(broken)  # 未闭合
        self.assertIsInstance(items, list)

    def test_title_fallback_to_detail_when_no_links(self):
        html = '<div class="row item-box"><small>640x480 JPEG 12KB</small></div>'
        items = parse_ascii2d_html(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["detail"], "640x480 JPEG 12KB")
        self.assertEqual(items[0]["title"], "640x480 JPEG 12KB", "无标题链接时 title 回退为 detail")
        # 无 detail 又无 title 时，build_result 兜底为 (无标题)
        self.assertEqual(build_result_from_item({}).title, "(无标题)")

    def test_title_noise_filtered(self):
        html = (
            '<div class="row item-box"><div class="detail-box gray-link">'
            '<small>foo</small><a href="http://x/1">詳細掲示板のログ だよ</a></div></div>'
        )
        items = parse_ascii2d_html(html)
        self.assertEqual(items[0]["title"], "", "含 2ch 噪声的标题应被置空")

    def test_h6_fallback_title(self):
        html = (
            '<div class="row item-box"><div class="detail-box gray-link">'
            '<h6>纯文本标题</h6><small>x</small></div></div>'
        )
        items = parse_ascii2d_html(html)
        self.assertEqual(items[0]["title"], "纯文本标题")

    def test_build_result_mapping(self):
        items = parse_ascii2d_html(SAMPLE_HTML)
        r0 = build_result_from_item(items[0])
        self.assertEqual(r0.source, "Pixiv")
        self.assertEqual(r0.title, "作品标题A")
        self.assertIsNone(r0.score)
        self.assertEqual(r0.extra["author"], "画师A")
        self.assertEqual(r0.extra["source_mark"], "pixiv")
        self.assertIn("detail", r0.extra)
        self.assertIn("hash", r0.extra)

    def test_build_result_unknown_source_and_empty(self):
        r = build_result_from_item({})
        self.assertEqual(r.title, "(无标题)")
        self.assertEqual(r.source, "ascii2d")
        self.assertEqual(r.url, "(无链接)")
        self.assertIsNone(r.score)

    def test_source_display_names_complete(self):
        for mark in ("fanbox", "fantia", "misskey", "pixiv", "twitter", "ニコニコ静画", "ニジエ"):
            self.assertIn(mark, SOURCE_DISPLAY_NAMES)


# ===========================================================================
# 3. score=None 不被过滤 + 顺序保持
# ===========================================================================
class TestNoSimilarityFiltering(unittest.TestCase):
    def test_results_all_score_none_and_kept(self):
        outcome = parse_ascii2d_response(SAMPLE_HTML)
        self.assertEqual(len(outcome.results), 2)
        self.assertTrue(all(r.score is None for r in outcome.results))
        # 经 formatter 仍照常展示（不会因「无相似度」被丢弃）
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="H")
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertIn("作品标题A", text)

    def test_order_preserved(self):
        outcome = parse_ascii2d_response(SAMPLE_HTML)
        self.assertEqual(outcome.results[0].title, "作品标题A")
        self.assertEqual(outcome.results[1].title, "tweet title")

    def test_max_results_slice(self):
        outcome = parse_ascii2d_response(SAMPLE_HTML, max_results=1)
        self.assertEqual(len(outcome.results), 1)

    def test_empty_html_warns(self):
        outcome = parse_ascii2d_response("")
        self.assertEqual(outcome.results, [])
        self.assertTrue(outcome.warnings)


# ===========================================================================
# 4. 双源并行 / 失败隔离 / 配置开关
# ===========================================================================
class TestDualSourceDispatch(unittest.TestCase):
    def _plugin(self, **cfg):
        return SoutuSearchPlugin(object(), cfg)

    def _img_plugin(self, **cfg):
        p = self._plugin(**cfg)

        async def fe(event):
            return ImagePayload(data=PNG, mime="image/png", filename="q.png")

        p.image_source.from_event = fe  # type: ignore[assignment]
        p.image_source.has_image = lambda ev: True  # type: ignore[assignment]
        return p

    def test_both_sources_run_parallel(self):
        p = self._img_plugin(saucenao_api_key="k")
        sa = _StubSaucenao()
        a2d = _StubAscii2d()
        p.saucenao = sa
        p.ascii2d = a2d
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        self.assertEqual(len(sa.calls), 1)
        self.assertEqual(len(a2d.calls), 1)
        self.assertEqual(out[0][0], "chain")
        text = _chain_text(out[0])
        self.assertIn("SauceNAO", text)
        self.assertIn("ascii2d", text)

    def test_ascii2d_failure_isolated(self):
        """ascii2d 抛异常，SauceNAO 结果照常展示。"""
        p = self._img_plugin(saucenao_api_key="k")
        sa = _StubSaucenao()
        a2d = _StubAscii2d(exc=RuntimeError("ascii2d boom"))
        p.saucenao = sa
        p.ascii2d = a2d
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        text = _chain_text(out[0])
        self.assertIn("snao", text, "SauceNAO 结果应照常展示")
        self.assertIn("ascii2d", text)
        self.assertIn("检索失败", text)
        self.assertIn("ascii2d boom", text)

    def test_saucenao_failure_isolated(self):
        """SauceNAO 抛异常，ascii2d 结果照常展示。"""
        p = self._img_plugin(saucenao_api_key="k")
        sa = _StubSaucenao(exc=RuntimeError("saucenao boom"))
        a2d = _StubAscii2d()
        p.saucenao = sa
        p.ascii2d = a2d
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        text = _chain_text(out[0])
        self.assertIn("a2d", text, "ascii2d 结果应照常展示")
        self.assertIn("SauceNAO", text)
        self.assertIn("saucenao boom", text)

    def test_both_fail_gives_explicit_messages(self):
        p = self._img_plugin(saucenao_api_key="k")
        p.saucenao = _StubSaucenao(exc=RuntimeError("sa down"))
        p.ascii2d = _StubAscii2d(exc=RuntimeError("a2d down"))
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        text = _chain_text(out[0])
        self.assertIn("检索失败", text)
        self.assertIn("sa down", text)
        self.assertIn("a2d down", text)

    def test_key_missing_still_runs_ascii2d(self):
        p = self._img_plugin()  # 无 key
        sa = _StubSaucenao()
        a2d = _StubAscii2d()
        p.saucenao = sa
        p.ascii2d = a2d
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        self.assertEqual(sa.calls, [])
        self.assertEqual(len(a2d.calls), 1)
        text = _chain_text(out[0])
        self.assertIn("跳过", text)
        self.assertIn("ascii2d", text)

    def test_saucenao_disabled_only_ascii2d(self):
        p = self._img_plugin(saucenao_enable=False)
        sa = _StubSaucenao()
        a2d = _StubAscii2d()
        p.saucenao = sa
        p.ascii2d = a2d
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        self.assertEqual(sa.calls, [])
        self.assertEqual(len(a2d.calls), 1)
        self.assertNotIn("SauceNAO】命中", _chain_text(out[0]))

    def test_ascii2d_disabled_only_saucenao(self):
        p = self._img_plugin(saucenao_api_key="k", ascii2d_enable=False)
        sa = _StubSaucenao()
        a2d = _StubAscii2d()
        p.saucenao = sa
        p.ascii2d = a2d
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        self.assertEqual(len(sa.calls), 1)
        self.assertEqual(a2d.calls, [])

    def test_both_disabled_config_hint(self):
        p = self._plugin(saucenao_enable=False, ascii2d_enable=False)
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        self.assertEqual(out[0][0], "plain")
        self.assertEqual(out[0][1], ALL_SOURCES_DISABLED_TEXT)

    def test_ascii2d_cache_namespace(self):
        """同图二次反查命中缓存（ascii2d.search 仅调用一次），且与 SauceNAO 命名空间隔离。"""
        p = self._img_plugin(saucenao_enable=False)
        a2d = _StubAscii2d()
        p.ascii2d = a2d
        collect(p._dispatch_saucenao(_FakeEvent(), ""))
        collect(p._dispatch_saucenao(_FakeEvent(), ""))
        self.assertEqual(len(a2d.calls), 1, "第二次应命中缓存")

    def test_cache_keys_are_distinct(self):
        data = PNG
        self.assertNotEqual(cache_mod.make_ascii2d_key(data), cache_mod.make_saucenao_key(data))
        self.assertNotEqual(cache_mod.make_ascii2d_key(data), cache_mod.make_image_key(data))
        self.assertNotEqual(
            cache_mod.make_ascii2d_key(data, bovw=True),
            cache_mod.make_ascii2d_key(data, bovw=False),
        )


# ===========================================================================
# 5. 新配置项与容错
# ===========================================================================
class TestNewConfig(unittest.TestCase):
    def test_defaults(self):
        p = SoutuSearchPlugin(object(), {})
        self.assertTrue(p.saucenao_enable)
        self.assertTrue(p.ascii2d_enable)
        self.assertEqual(p.ascii2d_base_url, "https://ascii2d.net")
        self.assertEqual(p.ascii2d.base_url, "https://ascii2d.net")
        self.assertFalse(p.ascii2d_bovw)

    def test_override_and_bool_coercion(self):
        p = SoutuSearchPlugin(object(), {
            "saucenao_enable": "false",
            "ascii2d_enable": "true",
            "ascii2d_base_url": "https://mirror.example.com",
            "ascii2d_bovw": "yes",
        })
        self.assertFalse(p.saucenao_enable)
        self.assertTrue(p.ascii2d_enable)
        self.assertEqual(p.ascii2d.base_url, "https://mirror.example.com")
        self.assertTrue(p.ascii2d_bovw)

    def test_invalid_types_fall_back(self):
        p = SoutuSearchPlugin(object(), {
            "saucenao_enable": None,
            "ascii2d_enable": object(),
            "ascii2d_base_url": "",
            "ascii2d_bovw": None,
        })
        self.assertTrue(p.saucenao_enable, "非法值应回退默认 True")
        self.assertTrue(p.ascii2d_enable, "非法值应回退默认 True")
        self.assertEqual(p.ascii2d_base_url, "https://ascii2d.net")
        self.assertFalse(p.ascii2d_bovw)

    def test_schema_has_new_keys(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(len(schema), 24)
        self.assertEqual(schema["saucenao_enable"]["default"], True)
        self.assertEqual(schema["ascii2d_enable"]["default"], True)
        self.assertEqual(schema["ascii2d_base_url"]["default"], "https://ascii2d.net")
        self.assertEqual(schema["ascii2d_bovw"]["default"], False)
        for key in ("saucenao_enable", "ascii2d_enable", "ascii2d_base_url", "ascii2d_bovw"):
            self.assertIn("description", schema[key])
            self.assertIn("hint", schema[key])


# ===========================================================================
# 6. NSFW 硬门：关闭时不产生 image 块、不泄漏缩略图 URL
# ===========================================================================
class TestNsfwGate(unittest.TestCase):
    def test_ascii2d_results_no_image_block_no_thumb_leak(self):
        outcome = parse_ascii2d_response(SAMPLE_HTML)
        # 每个结果都带缩略图，但 nsfw 关闭时既不发图片块，也不泄漏 URL
        self.assertTrue(any(r.thumbnail for r in outcome.results), "样例应带缩略图以验证不泄漏")
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="【ascii2d】H")
        self.assertFalse(any(b["type"] == "image" for b in blocks))
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertNotIn("thumb", joined)
        self.assertNotIn("ascii2d.net/thumb", joined)

    def test_dispatch_nsfw_off_no_image_block(self):
        p = SoutuSearchPlugin(object(), {"saucenao_enable": False, "nsfw_send_image": False})

        async def fe(event):
            return ImagePayload(data=PNG, mime="image/png", filename="q.png")

        p.image_source.from_event = fe  # type: ignore[assignment]
        p.image_source.has_image = lambda ev: True  # type: ignore[assignment]
        stub = _StubAscii2d(outcome=parse_ascii2d_response(SAMPLE_HTML))
        p.ascii2d = stub
        out = collect(p._dispatch_saucenao(_FakeEvent(), ""))
        self.assertEqual(out[0][0], "chain")
        comps = out[0][1]
        self.assertFalse(any(type(c).__name__.lower() == "image" for c in comps))
        joined = "\n".join(getattr(c, "text", "") for c in comps)
        self.assertNotIn("thumb", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
