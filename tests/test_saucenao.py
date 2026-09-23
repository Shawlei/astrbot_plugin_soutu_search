"""SauceNAO（`搜图` 的以图反查分支）provider 单元测试。

覆盖（依据 ``recon/API_SAUCENAO.md`` 规范，用**构造的响应样例**离线验证）：
- 数据库掩码：``96 == 0x20 | 0x40`` 显式断言、非法值回退、``0``（不限库）放行；
- ``min_similarity`` 越界回退、``hide`` 越界回退、``numres`` 夹取；
- 响应解析容错：``similarity`` 为字符串 / 非法值不崩；``header.status != 0`` 报错；
  ``results`` 为 ``[]`` / 缺失 / 非 list 优雅无结果；``data`` 结构因库而异；
- 链接回退链（ext_urls → pixiv_id → source → 无）；画师信息（creator/author_name/member）；
- 缩略图不外传文本；NSFW 关闭时不产生 image 块；
- 配额：``long_remaining`` / ``short_remaining`` 耗尽提示，字段缺失不报错；
- 网络层错误：401/403/429/500 / 超时 / 非 JSON / status 错误；
- 指令路由：``搜图``（别名 ``pixiv`` / ``saucenao``）识别，人话连读不误判，``搜图帮助`` 仍优先，
  ``搜P站`` 已不再是本插件指令；
- 访问控制覆盖新指令；api_key 缺失时**图片分支**给出引导且不下载、不发请求；
- 配置一致性 20 ↔ 20。

复用 tests/test_core.py 的 astrbot 桩（import 即完成 sys.modules 装配）。
运行::
    python -m unittest tests.test_saucenao -v
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search.core.saucenao_client import (  # noqa: E402
    DEFAULT_DB_MASK,
    DEFAULT_HIDE,
    DEFAULT_MIN_SIMILARITY,
    DB_DANBOORU,
    DB_PIXIV,
    DB_PIXIV_HISTORICAL,
    DB_TWITTER,
    DB_YANDERE,
    SaucenaoClient,
    build_result,
    describe_db_mask,
    parse_saucenao_response,
    resolve_db_mask,
    resolve_hide,
    resolve_min_similarity,
    resolve_numres,
)
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    ACCESS_DENIED_TEXT,
    HELP_TEXT,
    SAUCENAO_KEY_MISSING_TEXT,
    SAUCENAO_NO_IMAGE_TEXT,
    SoutuSearchPlugin,
    _command_head,
    _render_help,
)


def run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [x async for x in agen]


def collect(agen):
    return run(_collect(agen))


# --------------------------------------------------------------------------- #
# 构造响应样例（严格按文档结构）
# --------------------------------------------------------------------------- #
def make_payload(
    *,
    similarity="95.42",
    status=0,
    index_id=5,
    index_name="pixiv",
    data=None,
    thumbnail="https://img1.saucenao.com/res/pixiv/xxx.jpg?auth=abc&exp=123",
    short_remaining=3,
    long_remaining=148,
    short_limit="4",
    long_limit="150",
):
    if data is None:
        data = {
            "ext_urls": ["https://www.pixiv.net/artworks/12345678"],
            "title": "作品标题",
            "pixiv_id": 12345678,
            "member": "1234567",
            "creator": "画师名",
            "author_name": "画师名",
            "author_url": "https://www.pixiv.net/users/1234567",
        }
    header = {
        "status": status,
        "short_limit": short_limit,
        "long_limit": long_limit,
        "long_remaining": long_remaining,
        "short_remaining": short_remaining,
        "minimum_similarity": "30.00",
    }
    results = []
    if index_name is not None or similarity is not None:
        res_header = {}
        if similarity is not None:
            res_header["similarity"] = similarity
        if thumbnail is not None:
            res_header["thumbnail"] = thumbnail
        if index_id is not None:
            res_header["index_id"] = index_id
        if index_name is not None:
            res_header["index_name"] = index_name
        results.append({"header": res_header, "data": data})
    return {"header": header, "results": results}


# ===========================================================================
# 1. 数据库掩码 / 配置解析
# ===========================================================================
class TestDbMask(unittest.TestCase):
    def test_pixiv_mask_is_0x20_or_0x40(self):
        """显式断言：Pixiv 专用掩码 96 == 0x20 | 0x40。"""
        self.assertEqual(DB_PIXIV, 0x20)
        self.assertEqual(DB_PIXIV_HISTORICAL, 0x40)
        self.assertEqual(0x20 | 0x40, 0x60)
        self.assertEqual(0x60, 96)
        self.assertEqual(DEFAULT_DB_MASK, 0x20 | 0x40)
        self.assertEqual(DEFAULT_DB_MASK, 96)

    def test_common_masks(self):
        self.assertEqual(DB_DANBOORU, 512)
        self.assertEqual(DB_YANDERE, 4096)
        self.assertEqual(DB_TWITTER, 0x10000000000)

    def test_resolve_valid(self):
        self.assertEqual(resolve_db_mask(96), 96)
        self.assertEqual(resolve_db_mask("96"), 96)
        self.assertEqual(resolve_db_mask("0x60"), 96)
        self.assertEqual(resolve_db_mask(96.0), 96)

    def test_zero_means_all(self):
        self.assertEqual(resolve_db_mask(0), 0)

    def test_resolve_invalid_falls_back(self):
        for bad in (-1, -100, None, "abc", "", True, False, 96.5, {}, []):
            self.assertEqual(resolve_db_mask(bad), DEFAULT_DB_MASK, f"{bad!r} 应回退默认")

    def test_resolve_custom_default(self):
        self.assertEqual(resolve_db_mask(-1, default=7), 7)

    def test_describe_db_mask(self):
        text = describe_db_mask(96)
        self.assertIn("pixiv", text)
        self.assertIn("pixivhistorical", text)
        self.assertIn("全部库", describe_db_mask(0))
        self.assertIn("未识别", describe_db_mask(0b100000000000000000000000000000))


class TestMinSimilarity(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(resolve_min_similarity(50), 50)
        self.assertEqual(resolve_min_similarity(0), 0)
        self.assertEqual(resolve_min_similarity(100), 100)
        self.assertEqual(resolve_min_similarity("75"), 75)

    def test_out_of_range_falls_back(self):
        for bad in (-1, 101, 1000, -999):
            self.assertEqual(resolve_min_similarity(bad), DEFAULT_MIN_SIMILARITY, f"{bad!r}")

    def test_invalid_type_falls_back(self):
        for bad in (None, "abc", "", {}, [], True):
            self.assertEqual(resolve_min_similarity(bad), DEFAULT_MIN_SIMILARITY, f"{bad!r}")


class TestHide(unittest.TestCase):
    def test_valid(self):
        for v in (0, 1, 2, 3):
            self.assertEqual(resolve_hide(v), v)
        self.assertEqual(resolve_hide("2"), 2)

    def test_out_of_range_falls_back(self):
        for bad in (-1, 4, 99, None, "x", True, 1.5):
            self.assertEqual(resolve_hide(bad), DEFAULT_HIDE, f"{bad!r}")


class TestNumres(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(resolve_numres(15), 15)

    def test_clamped(self):
        self.assertEqual(resolve_numres(0), 1)
        self.assertEqual(resolve_numres(-5), 1)
        self.assertEqual(resolve_numres(999), 40)

    def test_invalid_default(self):
        self.assertEqual(resolve_numres(None), 15)
        self.assertEqual(resolve_numres("abc"), 15)


# ===========================================================================
# 2. 响应解析
# ===========================================================================
class TestParseSaucenao(unittest.TestCase):
    def test_non_dict_payload(self):
        outcome = parse_saucenao_response(["nope"])
        self.assertEqual(outcome.results, [])
        self.assertTrue(outcome.warnings)

    def test_similarity_string_to_float(self):
        outcome = parse_saucenao_response(make_payload(similarity="95.42"))
        self.assertEqual(len(outcome.results), 1)
        self.assertAlmostEqual(outcome.results[0].score, 95.42)

    def test_similarity_invalid_no_crash(self):
        outcome = parse_saucenao_response(make_payload(similarity="not-a-number"))
        self.assertEqual(len(outcome.results), 1)
        self.assertIsNone(outcome.results[0].score)

    def test_similarity_missing_no_crash(self):
        outcome = parse_saucenao_response(make_payload(similarity=None))
        self.assertIsNone(outcome.results[0].score)

    def test_status_nonzero_is_error(self):
        outcome = parse_saucenao_response(make_payload(status=2, similarity=None, index_name=None))
        self.assertEqual(outcome.results, [])
        self.assertTrue(outcome.meta.get("status_error"))
        self.assertTrue(any("状态码 2" in w for w in outcome.warnings))

    def test_results_empty(self):
        payload = make_payload()
        payload["results"] = []
        outcome = parse_saucenao_response(payload)
        self.assertEqual(outcome.results, [])
        self.assertFalse(outcome.meta.get("status_error"))

    def test_results_missing(self):
        payload = make_payload()
        del payload["results"]
        outcome = parse_saucenao_response(payload)
        self.assertEqual(outcome.results, [])

    def test_results_non_list(self):
        payload = make_payload()
        payload["results"] = {"not": "a list"}
        outcome = parse_saucenao_response(payload)
        self.assertEqual(outcome.results, [])

    def test_item_non_dict_skipped(self):
        payload = make_payload()
        payload["results"] = ["x", None, 42]
        outcome = parse_saucenao_response(payload)
        self.assertEqual(outcome.results, [])

    def test_data_missing(self):
        payload = make_payload()
        payload["results"][0]["data"] = None
        outcome = parse_saucenao_response(payload)
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.results[0].url, "")

    def test_header_missing(self):
        payload = {"results": [{"data": {"title": "t"}}]}
        outcome = parse_saucenao_response(payload)
        self.assertEqual(len(outcome.results), 1)
        self.assertIsNone(outcome.results[0].score)

    # ---- 链接回退链 ----
    def test_link_ext_urls_first(self):
        r = parse_saucenao_response(make_payload()).results[0]
        self.assertEqual(r.url, "https://www.pixiv.net/artworks/12345678")

    def test_link_fallback_pixiv_id(self):
        data = {"pixiv_id": 999, "member": "1"}
        r = parse_saucenao_response(make_payload(data=data)).results[0]
        self.assertEqual(r.url, "https://www.pixiv.net/artworks/999")

    def test_link_fallback_source(self):
        data = {"source": "https://example.com/orig"}
        r = parse_saucenao_response(make_payload(data=data)).results[0]
        self.assertEqual(r.url, "https://example.com/orig")

    def test_link_none(self):
        r = parse_saucenao_response(make_payload(data={})).results[0]
        self.assertEqual(r.url, "")

    def test_link_pixiv_id_string(self):
        data = {"pixiv_id": "123456", "ext_urls": []}
        r = parse_saucenao_response(make_payload(data=data)).results[0]
        self.assertEqual(r.url, "https://www.pixiv.net/artworks/123456")

    def test_ext_urls_non_list_ignored(self):
        data = {"ext_urls": "https://x/y", "pixiv_id": 5}
        r = parse_saucenao_response(make_payload(data=data)).results[0]
        self.assertEqual(r.url, "https://www.pixiv.net/artworks/5")

    # ---- 画师信息 ----
    def test_artist_creator(self):
        r = parse_saucenao_response(make_payload()).results[0]
        self.assertEqual(r.extra["artist"], "画师名")
        self.assertEqual(r.extra["artist_url"], "https://www.pixiv.net/users/1234567")

    def test_artist_author_name(self):
        data = {"author_name": "作者甲", "member": "7"}
        r = parse_saucenao_response(make_payload(data=data)).results[0]
        self.assertEqual(r.extra["artist"], "作者甲")

    def test_artist_member_fallback(self):
        data = {"member": "555"}
        r = parse_saucenao_response(make_payload(data=data)).results[0]
        self.assertEqual(r.extra["artist"], "555")
        self.assertEqual(r.extra["artist_url"], "https://www.pixiv.net/users/555")

    def test_no_artist(self):
        r = parse_saucenao_response(make_payload(data={})).results[0]
        self.assertIsNone(r.extra["artist"])

    # ---- 库名 ----
    def test_source_from_index_name(self):
        r = parse_saucenao_response(make_payload(index_name="pixiv")).results[0]
        self.assertIn("pixiv", r.source)
        self.assertEqual(r.extra["library"], "pixiv")

    def test_source_from_index_id_fallback(self):
        r = parse_saucenao_response(make_payload(index_id=9, index_name=None)).results[0]
        self.assertEqual(r.extra["library"], "danbooru")
        self.assertIn("danbooru", r.source)

    # ---- 缩略图 ----
    def test_thumbnail_passthrough_not_in_title(self):
        r = parse_saucenao_response(make_payload()).results[0]
        self.assertIsNotNone(r.thumbnail)
        self.assertIn("auth=", r.thumbnail)
        self.assertNotIn("auth=", r.title)

    # ---- 排序 / 过滤 ----
    def test_sorted_desc_and_filtered(self):
        payload = make_payload()
        first = {"header": {"similarity": "90.0", "index_id": 5, "index_name": "pixiv"}, "data": {"title": "a"}}
        second = {"header": {"similarity": "70.0", "index_id": 5, "index_name": "pixiv"}, "data": {"title": "b"}}
        third = {"header": {"similarity": "10.0", "index_id": 5, "index_name": "pixiv"}, "data": {"title": "c"}}
        payload["results"] = [third, first, second]
        outcome = parse_saucenao_response(payload, min_similarity=50)
        scores = [r.score for r in outcome.results]
        self.assertEqual(scores, [90.0, 70.0])  # 降序 + 过滤掉 10.0

    def test_low_confidence_flag_and_warning(self):
        # score 为 None（解析失败）时结果被保留并标记低置信度，触发提示
        outcome = parse_saucenao_response(make_payload(similarity="abc"), min_similarity=50)
        self.assertEqual(len(outcome.results), 1)
        self.assertTrue(outcome.results[0].extra["low_confidence"])
        self.assertTrue(any("低于阈值" in w for w in outcome.warnings))

    # ---- 配额 ----
    def test_quota_long_exhausted_warning(self):
        outcome = parse_saucenao_response(make_payload(long_remaining=0))
        self.assertTrue(any("今日" in w and "配额" in w for w in outcome.warnings))

    def test_quota_short_exhausted_warning(self):
        outcome = parse_saucenao_response(make_payload(short_remaining=0))
        self.assertTrue(any("限流" in w for w in outcome.warnings))

    def test_quota_missing_no_crash_no_warning(self):
        payload = make_payload()
        del payload["header"]["long_remaining"]
        del payload["header"]["short_remaining"]
        outcome = parse_saucenao_response(payload)
        self.assertFalse(any("配额" in w or "限流" in w for w in outcome.warnings))
        self.assertIsNone(outcome.meta["quota"]["long_remaining"])

    def test_quota_meta_recorded(self):
        outcome = parse_saucenao_response(make_payload(long_remaining=100, short_remaining=2))
        self.assertEqual(outcome.meta["quota"]["long_remaining"], 100)
        self.assertEqual(outcome.meta["quota"]["short_remaining"], 2)

    # ---- build_result 直接调用 ----
    def test_build_result_non_dict(self):
        self.assertIsNone(build_result("nope"))  # type: ignore[arg-type]


# ===========================================================================
# 3. 网络层错误（假 session）
# ===========================================================================
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


def make_client(resp=None, **kw):
    c = SaucenaoClient(timeout=30, **kw)
    if resp is not None:
        c._session = FakeSession(resp)
    return c


class TestSaucenaoNetwork(unittest.TestCase):
    def _raises(self, coro):
        try:
            run(coro)
        except Exception as exc:  # noqa: BLE001
            return exc
        self.fail("预期抛异常但未抛")

    def test_http_401(self):
        exc = self._raises(make_client(FakeResp(401, "no")).search(b"img"))
        self.assertIn("401", str(exc))

    def test_http_403(self):
        exc = self._raises(make_client(FakeResp(403, "cf")).search(b"img"))
        self.assertIn("403", str(exc))

    def test_http_429(self):
        exc = self._raises(make_client(FakeResp(429, "slow")).search(b"img"))
        self.assertIn("429", str(exc))

    def test_http_500(self):
        exc = self._raises(make_client(FakeResp(500, "err")).search(b"img"))
        self.assertIn("500", str(exc))

    def test_timeout(self):
        exc = self._raises(make_client(FakeResp(200, "", raise_exc=asyncio.TimeoutError())).search(b"img"))
        self.assertIsInstance(exc, RuntimeError)

    def test_non_json(self):
        exc = self._raises(make_client(FakeResp(200, "<html>challenge</html>")).search(b"img"))
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("非 JSON", str(exc))

    def test_status_error_raises(self):
        body = json.dumps(make_payload(status=3, similarity=None, index_name=None))
        exc = self._raises(make_client(FakeResp(200, body)).search(b"img"))
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("状态码 3", str(exc))

    def test_empty_image(self):
        exc = self._raises(make_client(FakeResp(200, "{}")).search(b""))
        self.assertIsInstance(exc, ValueError)

    def test_valid_response(self):
        body = json.dumps(make_payload())
        outcome = run(make_client(FakeResp(200, body)).search(b"img"))
        self.assertEqual(len(outcome.results), 1)
        self.assertAlmostEqual(outcome.results[0].score, 95.42)

    def test_query_params(self):
        body = json.dumps(make_payload())
        c = make_client(FakeResp(200, body), api_key="KEY123", db_mask=96, min_similarity=60, hide=1)
        run(c.search(b"img", filename="q.png", mime="image/png"))
        params = c._session.last["params"]
        self.assertEqual(params["output_type"], "2")
        self.assertEqual(params["dbmask"], "96")
        self.assertEqual(params["minsim"], "60")
        self.assertEqual(params["hide"], "1")
        self.assertEqual(params["api_key"], "KEY123")
        self.assertEqual(c._session.last["url"], "https://saucenao.com/search.php")

    def test_query_params_without_api_key(self):
        body = json.dumps(make_payload())
        c = make_client(FakeResp(200, body), api_key="")
        run(c.search(b"img"))
        self.assertNotIn("api_key", c._session.last["params"])

    def test_headers_referer_and_ua(self):
        body = json.dumps(make_payload())
        c = make_client(FakeResp(200, body))
        run(c.search(b"img"))
        headers = c._session.last["headers"]
        self.assertEqual(headers["Referer"], "https://saucenao.com/")
        self.assertIn("Mozilla", headers["User-Agent"])

    def test_referer_follows_base_url(self):
        body = json.dumps(make_payload())
        c = make_client(FakeResp(200, body), base_url="https://mirror.example.com/")
        run(c.search(b"img"))
        self.assertEqual(c._session.last["headers"]["Referer"], "https://mirror.example.com/")
        self.assertEqual(c._session.last["url"], "https://mirror.example.com/search.php")


# ===========================================================================
# 4. 插件配置解析
# ===========================================================================
class TestPluginSaucenaoConfig(unittest.TestCase):
    def _plugin(self, cfg):
        return SoutuSearchPlugin(object(), cfg)

    def test_defaults(self):
        p = self._plugin({})
        self.assertEqual(p.saucenao_api_key, "")
        self.assertEqual(p.saucenao_base_url, "https://saucenao.com")
        self.assertEqual(p.saucenao_db_mask, 96)
        self.assertEqual(p.saucenao_min_similarity, 50)
        self.assertEqual(p.saucenao_hide, 0)

    def test_custom(self):
        p = self._plugin({
            "saucenao_api_key": "abc",
            "saucenao_base_url": "https://mirror",
            "saucenao_db_mask": 0,
            "saucenao_min_similarity": 30,
            "saucenao_hide": 3,
        })
        self.assertEqual(p.saucenao_api_key, "abc")
        self.assertEqual(p.saucenao_base_url, "https://mirror")
        self.assertEqual(p.saucenao.base_url, "https://mirror")
        self.assertEqual(p.saucenao_db_mask, 0)
        self.assertEqual(p.saucenao_min_similarity, 30)
        self.assertEqual(p.saucenao_hide, 3)

    def test_invalid_fallbacks(self):
        p = self._plugin({
            "saucenao_db_mask": -5,
            "saucenao_min_similarity": 200,
            "saucenao_hide": 9,
        })
        self.assertEqual(p.saucenao_db_mask, 96)
        self.assertEqual(p.saucenao_min_similarity, 50)
        self.assertEqual(p.saucenao_hide, 0)

    def test_client_mirrors_config(self):
        p = self._plugin({"saucenao_db_mask": 512, "saucenao_min_similarity": 70})
        self.assertEqual(p.saucenao.db_mask, 512)
        self.assertEqual(p.saucenao.min_similarity, 70)


# ===========================================================================
# 5. 指令路由与访问控制
# ===========================================================================
class FakeEvent:
    def __init__(self, umo="umo-A", group_id=None, sender_id=None, text="", with_message_obj=True):
        self.unified_msg_origin = umo
        self.message_str = text
        if with_message_obj:
            self.message_obj = type("M", (), {"message_id": "m1"})()
        else:
            self.message_obj = None
        self._sender_id = sender_id
        self._group_id = group_id
        if group_id is not None and self.message_obj is not None:
            self.message_obj.group_id = group_id

    def get_message_str(self):
        return self.message_str

    def get_sender_id(self):
        if self._sender_id is None:
            raise RuntimeError("no sender id")
        return self._sender_id

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, comps):
        return ("chain", comps)


class TestCommandRouting(unittest.TestCase):
    def test_search_and_alias_commands_recognized(self):
        for t in ("/搜图", "搜图", "#搜图", "/pixiv", "/saucenao", "#pixiv",
                  "/搜图帮助", "搜图帮助x", "/pixiv 猫娘", "/搜图 http://x/a.jpg",
                  "/pixivhelp", "/saucenaohelp"):
            self.assertIsNotNone(_command_head(t), f"应识别为指令: {t!r}")

    def test_removed_pixiv_cmd_is_not_command(self):
        """0.5.0 起 ``搜P站`` / ``搜P站帮助`` 已移除。"""
        for t in ("/搜P站", "搜P站", "#搜P站", "/搜P站帮助", "搜P站帮助x", "/搜P站 猫娘"):
            self.assertIsNone(_command_head(t), f"不应识别为指令: {t!r}")

    def test_human_continuation_not_command(self):
        for t in ("搜图真有意思", "pixiv真好用", "saucenao很好用", "pixiv…",
                  "pixiv很好用", "saucenao不错", "搜图帮助…", "soutubot很棒"):
            self.assertIsNone(_command_head(t), f"不应识别为指令（人话）: {t!r}")

    def test_alias_ascii_ok(self):
        # 纯 ASCII 别名 #pixiv 正确识别
        self.assertEqual(_command_head("#pixiv", ["#"]), "pixiv")
        self.assertEqual(_command_head("#saucenao", ["#"]), "saucenao")
        self.assertEqual(_command_head("#pixivhelp", ["#"]), "pixivhelp")

    def test_help_preferred_over_body(self):
        self.assertEqual(_command_head("/搜图帮助"), "搜图帮助")
        self.assertEqual(_command_head("/搜图"), "搜图")

    def test_alias_with_cjk_arg(self):
        self.assertEqual(_command_head("/pixiv 猫娘"), "pixiv")
        self.assertEqual(_command_head("/saucenao 猫娘"), "saucenao")


class TestSaucenaoPluginCommands(unittest.TestCase):
    def _plugin(self, cfg=None):
        return SoutuSearchPlugin(object(), cfg or {})

    def test_api_key_missing_guidance_no_download(self):
        """未配置 api_key：图片分支回 key 引导，且**零下载**。"""
        p = self._plugin({})
        downloaded = []

        async def boom_from_event(event):
            downloaded.append("<from_event>")
            return None

        p.image_source.from_event = boom_from_event  # type: ignore[assignment]
        p.image_source.has_image = lambda ev: True  # type: ignore[assignment]
        out = collect(p.sou_cmd(FakeEvent()))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "plain")
        self.assertEqual(out[0][1], SAUCENAO_KEY_MISSING_TEXT)
        self.assertIn("user.php?page=search-api", out[0][1])
        self.assertEqual(downloaded, [], "未配置 key 时绝不能下载图片")

    def test_access_control_covers_sou_cmd(self):
        p = self._plugin({"access_mode": "whitelist", "whitelist": [], "saucenao_api_key": "k"})
        out = collect(p.sou_cmd(FakeEvent(umo="umo-A")))
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)

    def test_sou_cmd_no_image_no_args_gives_help(self):
        p = self._plugin({"saucenao_api_key": "k"})
        out = collect(p.sou_cmd(FakeEvent()))
        self.assertEqual(out[0][1], HELP_TEXT)

    def test_saucenao_no_image_fallback_still_reachable(self):
        """结构上有图片、但取不到可用图片且非直链 → 回 SAUCENAO_NO_IMAGE_TEXT。"""
        p = self._plugin({"saucenao_api_key": "k"})

        async def none_from_event(event):
            return None

        p.image_source.from_event = none_from_event  # type: ignore[assignment]
        out = collect(p._dispatch_saucenao(FakeEvent(), ""))
        self.assertEqual(out[0][1], SAUCENAO_NO_IMAGE_TEXT.format(p="/"))

    def test_help_subcommand(self):
        p = self._plugin({"saucenao_api_key": "k"})
        out = collect(p.sou_cmd(FakeEvent(), args="帮助"))
        self.assertIn("/搜图", out[0][1])

    def test_sou_help_cmd(self):
        p = self._plugin({})
        out = collect(p.sou_help_cmd(FakeEvent()))
        self.assertEqual(out[0][1], HELP_TEXT)

    def test_sou_help_respects_prefix(self):
        class Ctx:
            def get_config(self):
                return {"wake_prefix": ["#"]}
        p = SoutuSearchPlugin(Ctx(), {})
        self.assertIn("#搜图", p._help_text())
        self.assertNotIn("/搜图", p._help_text())

    def test_sou_help_cmd_access_denied(self):
        p = self._plugin({"access_mode": "whitelist", "whitelist": []})
        out = collect(p.sou_help_cmd(FakeEvent(umo="umo-A")))
        self.assertEqual(out[0][1], ACCESS_DENIED_TEXT)


# ===========================================================================
# 6. 帮助文案
# ===========================================================================
class TestSaucenaoHelp(unittest.TestCase):
    def test_default_constant(self):
        self.assertIn("/搜图", HELP_TEXT)
        self.assertIn("/搜图帮助", HELP_TEXT)
        self.assertNotIn("搜P站", HELP_TEXT)

    def test_render_with_prefix(self):
        text = _render_help("#")
        self.assertIn("#搜图", text)
        self.assertNotIn("/搜图", text)

    def test_render_empty_falls_back(self):
        self.assertIn("/搜图", _render_help(""))

    def test_main_help_mentions_both_branches(self):
        from astrbot_plugin_soutu_search.main import HELP_TEXT as HT
        self.assertIn("SauceNAO", HT)
        self.assertIn("Safebooru", HT)
        self.assertIn("/搜本", HT)


# ===========================================================================
# 7. 格式化（画师展示 / NSFW 约束）
# ===========================================================================
class TestFormatterArtist(unittest.TestCase):
    def test_artist_rendered(self):
        from astrbot_plugin_soutu_search.core.formatter import (
            SearchResult,
            SourceOutcome,
            format_outcome,
        )
        outcome = SourceOutcome(results=[
            SearchResult(title="作品", source="来自 pixiv 库", url="https://x/y",
                         score=95.4, extra={"artist": "画师名"})
        ])
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="H")
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertIn("画师名", text)

    def test_nsfw_off_no_image_block_and_no_thumb_leak(self):
        from astrbot_plugin_soutu_search.core.formatter import (
            SearchResult,
            SourceOutcome,
            format_outcome,
        )
        outcome = SourceOutcome(results=[
            SearchResult(title="作品", source="来自 pixiv 库", url="https://x/y",
                         thumbnail="https://cdn.saucenao/secret.jpg?auth=1&exp=2",
                         score=95.4, extra={"artist": "画师名"})
        ])
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="H")
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertFalse(any(b["type"] == "image" for b in blocks))
        self.assertNotIn("secret.jpg", joined)


# ===========================================================================
# 8. 配置一致性 20 ↔ 20
# ===========================================================================
class TestConfigConsistency(unittest.TestCase):
    def _schema(self):
        return json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_schema_has_20_keys(self):
        self.assertEqual(len(self._schema()), 20)

    def test_new_keys_present_with_defaults(self):
        schema = self._schema()
        self.assertEqual(schema["saucenao_api_key"]["default"], "")
        self.assertTrue(schema["saucenao_api_key"].get("secret"))
        self.assertEqual(schema["saucenao_base_url"]["default"], "https://saucenao.com")
        self.assertEqual(schema["saucenao_db_mask"]["default"], 96)
        self.assertEqual(schema["saucenao_min_similarity"]["default"], 50)
        self.assertEqual(schema["saucenao_hide"]["default"], 0)

    def test_schema_and_code_usage_match(self):
        schema = set(self._schema().keys())
        src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        used = set(re.findall(r'\bcfg\.get\(\s*"([^"]+)"', src))
        used |= set(re.findall(r'\bself\.config\.get\(\s*"([^"]+)"', src))
        self.assertEqual(used - schema, set(), f"用了但未定义: {used - schema}")
        self.assertEqual(schema - used, set(), f"定义了但未使用: {schema - used}")

    def test_db_mask_default_is_pixiv_only(self):
        schema = self._schema()
        self.assertEqual(schema["saucenao_db_mask"]["default"], 0x20 | 0x40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
