"""QA 独立对抗测试（Edward）：SauceNAO（`搜图` 的以图反查分支）provider。

本文件由 QA 独立编写，**不依赖**被测方的 `tests/test_saucenao.py` 断言，
目的是证伪/确认以下离线可验证的关键点：

- 请求构造：POST、URL 路径、multipart 字段名（`file`）、查询参数、Referer/UA、base_url 生效；
- 解析容错：`similarity` 字符串/非法值、`status != 0`、`results` 形态、`data` 因库而异、
  链接回退链、画师回退、命中库、缺 header/data；
- 掩码独立计算：`96 == 0x20 | 0x40`，以及 0/非法/越界；
- 配额耗尽提示 + api_key 为空时**不发出任何网络请求**（拦截计数）；
- 指令识别与访问控制；
- 回归：formatter 无 artist 时不改变旧格式、缓存键命名空间不污染、terminate 关闭 saucenao 会话。

运行::
    python -m unittest tests.test_saucenao_qa_adversarial -v
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

from astrbot_plugin_soutu_search.core import cache as cache_mod  # noqa: E402
from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.saucenao_client import (  # noqa: E402
    DEFAULT_DB_MASK,
    SaucenaoClient,
    build_result,
    describe_db_mask,
    parse_saucenao_response,
    resolve_db_mask,
    resolve_hide,
    resolve_min_similarity,
)
from astrbot_plugin_soutu_search.core.image_source import ImagePayload  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    ACCESS_DENIED_TEXT,
    HELP_TEXT,
    SAUCENAO_KEY_MISSING_TEXT,
    SoutuSearchPlugin,
    _command_head,
)


def run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [x async for x in agen]


def collect(agen):
    return run(_collect(agen))


def _text_of(result) -> str:
    """从 ``("plain", text)`` / ``("chain", comps)`` 结果中提取纯文本。"""
    kind, payload = result
    if kind == "plain":
        return payload
    return "\n".join(getattr(c, "text", "") for c in payload)


class _StubAscii2dRecord:
    """ascii2d provider 替身（仅记录调用，避免测试触发真实联网）。"""

    def __init__(self):
        self.calls = []

    async def search(self, image, **kw):
        self.calls.append(kw)
        return SourceOutcome(
            results=[SearchResult(title="a2d", source="Pixiv", url="https://www.pixiv.net/artworks/1", score=None)]
        )

    async def close(self):
        pass


# --------------------------------------------------------------------------- #
# 请求拦截基建：捕获 FormData 表单字段 + 查询参数 + 方法
# --------------------------------------------------------------------------- #
def _extract_form_fields(form) -> list[tuple[str | None, object]]:
    """稳健地抽取 aiohttp.FormData 的 (字段名, 值)。

    兼容 aiohttp 多个版本的 `_fields` 结构（dict 形态 / tuple 形态）。
    """
    out: list[tuple[str | None, object]] = []
    for entry in getattr(form, "_fields", []):
        if isinstance(entry, dict):
            out.append((entry.get("name"), entry.get("value")))
        elif isinstance(entry, tuple):
            info = entry[0]
            name = info.get("name") if hasattr(info, "get") else None
            out.append((name, entry[-1]))
        else:
            out.append((None, None))
    return out


class RecordingResp:
    def __init__(self, status=200, body="{}"):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class RecordingSession:
    """记录每次 post 的 url/params/headers/data，并可自定义响应。"""

    def __init__(self, resp=None):
        self.closed = False
        self.calls: list[dict] = []
        self._resp = resp or RecordingResp(200, json.dumps(_ok_payload()))

    def post(self, url, **kw):
        self.calls.append({"url": url, "method": "POST", **kw})
        return self._resp

    def get(self, url, **kw):
        self.calls.append({"url": url, "method": "GET", **kw})
        raise AssertionError("SauceNAO 客户端不应使用 GET")


def _ok_payload(**override):
    header = {
        "status": 0,
        "short_limit": "4",
        "long_limit": "150",
        "long_remaining": 148,
        "short_remaining": 3,
    }
    header.update(override.pop("header", {}))
    payload = {
        "header": header,
        "results": [
            {
                "header": {"similarity": "95.42", "index_id": 5, "index_name": "pixiv",
                           "thumbnail": "https://img1.saucenao.com/res/x.jpg?auth=a&exp=1"},
                "data": {
                    "ext_urls": ["https://www.pixiv.net/artworks/123"],
                    "title": "T", "pixiv_id": 123, "member": "77", "creator": "c",
                },
            }
        ],
    }
    payload.update(override)
    return payload


# ===========================================================================
# B. 请求构造
# ===========================================================================
class TestRequestConstruction(unittest.TestCase):
    def _search(self, client):
        return run(client.search(b"\xff\xd8\xffIMG", filename="q.jpg", mime="image/jpeg"))

    def test_url_path_and_post(self):
        c = SaucenaoClient(base_url="https://saucenao.com", api_key="K")
        sess = RecordingSession()
        c._session = sess
        self._search(c)
        self.assertEqual(len(sess.calls), 1, "应恰好发出一次请求")
        self.assertEqual(sess.calls[0]["method"], "POST")
        self.assertEqual(sess.calls[0]["url"], "https://saucenao.com/search.php")

    def test_multipart_field_name_is_file(self):
        """multipart 字段名必须为 `file`（规范要求；未经真机实测，标注待确认）。"""
        c = SaucenaoClient(api_key="K")
        sess = RecordingSession()
        c._session = sess
        self._search(c)
        fields = _extract_form_fields(sess.calls[0]["data"])
        names = [n for n, _ in fields]
        self.assertEqual(names, ["file"], f"表单字段名应为 ['file']，实际 {names!r}")
        # 值应为图片二进制
        self.assertEqual(fields[0][1], b"\xff\xd8\xffIMG")

    def test_query_params_complete(self):
        c = SaucenaoClient(api_key="K", db_mask=96, min_similarity=60, hide=1)
        sess = RecordingSession()
        c._session = sess
        self._search(c)
        params = sess.calls[0]["params"]
        self.assertEqual(params["output_type"], "2")
        self.assertEqual(params["dbmask"], "96")
        self.assertEqual(params["minsim"], "60")
        self.assertEqual(params["hide"], "1")
        self.assertEqual(params["api_key"], "K")
        self.assertIn("numres", params)

    def test_no_api_key_param_when_empty(self):
        c = SaucenaoClient(api_key="")
        sess = RecordingSession()
        c._session = sess
        self._search(c)
        self.assertNotIn("api_key", sess.calls[0]["params"])

    def test_headers_ua_and_referer(self):
        c = SaucenaoClient(base_url="https://saucenao.com")
        sess = RecordingSession()
        c._session = sess
        self._search(c)
        headers = sess.calls[0]["headers"]
        self.assertEqual(headers["Referer"], "https://saucenao.com/")
        self.assertIn("Mozilla", headers["User-Agent"])

    def test_base_url_override_affects_url_and_referer(self):
        c = SaucenaoClient(base_url="https://my-mirror.example.com/", api_key="K")
        sess = RecordingSession()
        c._session = sess
        self._search(c)
        self.assertEqual(sess.calls[0]["url"], "https://my-mirror.example.com/search.php")
        self.assertEqual(sess.calls[0]["headers"]["Referer"], "https://my-mirror.example.com/")

    def test_body_is_multipart_formdata(self):
        import aiohttp
        c = SaucenaoClient(api_key="K")
        sess = RecordingSession()
        c._session = sess
        self._search(c)
        form = sess.calls[0]["data"]
        self.assertIsInstance(form, aiohttp.FormData, "请求体应为 multipart FormData")

    def test_empty_image_raises_before_request(self):
        c = SaucenaoClient(api_key="K")
        sess = RecordingSession()
        c._session = sess
        with self.assertRaises(ValueError):
            run(c.search(b""))
        self.assertEqual(len(sess.calls), 0, "空图片不应发出请求")


# ===========================================================================
# C. 解析容错
# ===========================================================================
class TestParseTolerance(unittest.TestCase):
    def _one(self, payload, **kw):
        out = parse_saucenao_response(payload, **kw)
        return out

    def test_similarity_string(self):
        r = self._one(_ok_payload()).results[0]
        self.assertAlmostEqual(r.score, 95.42)

    def test_similarity_int(self):
        p = _ok_payload()
        p["results"][0]["header"]["similarity"] = 88
        r = parse_saucenao_response(p).results[0]
        self.assertEqual(r.score, 88.0)

    def test_similarity_invalid_variants_no_crash(self):
        # 非法/缺失值不得抛异常、不得丢失结果项；应得 score=None（True 为 float(True)=1.0）
        for bad in ("abc", None, "", "  ", [], {}, "95.4x"):
            p = _ok_payload()
            p["results"][0]["header"]["similarity"] = bad
            out = parse_saucenao_response(p)
            self.assertEqual(len(out.results), 1, f"{bad!r} 不应导致结果丢失/崩溃")
            self.assertIsNone(out.results[0].score, f"{bad!r} 应得 None")
        # True 是 bool：float(True)=1.0，且 1.0 < 默认阈值 50 → 被过滤（但不崩）
        p = _ok_payload()
        p["results"][0]["header"]["similarity"] = True
        out = parse_saucenao_response(p)
        self.assertEqual(out.results, [])

    def test_similarity_negative_and_over100_no_crash(self):
        for val in (-5.0, 150.0):
            p = _ok_payload()
            p["results"][0]["header"]["similarity"] = val
            out = parse_saucenao_response(p, min_similarity=50)
            # 负分被 min 过滤掉；超 100 保留且不崩
            if val < 0:
                self.assertEqual(out.results, [])
            else:
                self.assertEqual(len(out.results), 1)
                self.assertEqual(out.results[0].score, 150.0)

    def test_status_nonzero_readable_error_not_silent(self):
        p = {"header": {"status": 2}, "results": []}
        out = parse_saucenao_response(p)
        self.assertTrue(out.meta.get("status_error"))
        self.assertEqual(out.results, [])
        self.assertTrue(any("状态码 2" in w for w in out.warnings))
        self.assertTrue(any("2" in w for w in out.warnings))

    def test_status_as_string(self):
        out = parse_saucenao_response({"header": {"status": "3"}, "results": []})
        self.assertTrue(out.meta.get("status_error"))

    def test_results_variants(self):
        for bad in ([], None, "x", {}, 42):
            out = parse_saucenao_response({"header": {"status": 0}, "results": bad})
            self.assertEqual(out.results, [], f"results={bad!r} 应优雅无结果")

    def test_results_missing(self):
        out = parse_saucenao_response({"header": {"status": 0}})
        self.assertEqual(out.results, [])

    def test_result_item_non_dict(self):
        out = parse_saucenao_response({"header": {"status": 0}, "results": ["x", None, 5, []]})
        self.assertEqual(out.results, [])

    def test_missing_header_and_data_no_crash(self):
        out = parse_saucenao_response({"results": [{"data": None}, {"header": None}, {}]})
        self.assertEqual(len(out.results), 3)
        for r in out.results:
            self.assertIsNone(r.score)

    def test_data_structures_per_library(self):
        # pixiv
        p = _ok_payload()
        p["results"][0]["data"] = {"pixiv_id": 999, "member": "11", "creator": "画师X"}
        r = parse_saucenao_response(p).results[0]
        self.assertEqual(r.url, "https://www.pixiv.net/artworks/999")
        self.assertEqual(r.extra["artist"], "画师X")
        # booru 类
        p2 = _ok_payload()
        p2["results"][0]["header"]["index_id"] = 9
        p2["results"][0]["header"]["index_name"] = "danbooru"
        p2["results"][0]["data"] = {"source": "https://danbooru.donmai.us/posts/1", "material": "x"}
        r2 = parse_saucenao_response(p2).results[0]
        self.assertEqual(r2.url, "https://danbooru.donmai.us/posts/1")
        self.assertEqual(r2.extra["library"], "danbooru")
        self.assertEqual(r2.extra["material"], "x")
        # 书籍类
        p3 = _ok_payload()
        p3["results"][0]["data"] = {"part": "1", "year": "2020"}
        r3 = parse_saucenao_response(p3).results[0]
        self.assertEqual(r3.extra["part"], "1")
        self.assertEqual(r3.extra["year"], "2020")

    # ---- 链接回退链 ----
    def test_link_fallback_chain(self):
        cases = [
            ({"ext_urls": ["https://ext/1"], "pixiv_id": 5, "source": "https://s"}, "https://ext/1"),
            ({"pixiv_id": 5, "source": "https://s"}, "https://www.pixiv.net/artworks/5"),
            ({"source": "https://s"}, "https://s"),
            ({}, ""),
            ({"ext_urls": []}, ""),
            ({"ext_urls": [None, 5, "  "], "pixiv_id": 7}, "https://www.pixiv.net/artworks/7"),
        ]
        for data, want in cases:
            p = _ok_payload()
            p["results"][0]["data"] = data
            r = parse_saucenao_response(p).results[0]
            self.assertEqual(r.url, want, f"data={data!r}")

    # ---- 画师回退 ----
    def test_artist_fallback_chain(self):
        cases = [
            ({"creator": "C", "author_name": "A", "member": "9"}, "C"),
            ({"author_name": "A", "member": "9"}, "A"),
            ({"member": "9"}, "9"),
            ({}, None),
            ({"member": None}, None),
        ]
        for data, want in cases:
            p = _ok_payload()
            p["results"][0]["data"] = data
            r = parse_saucenao_response(p).results[0]
            self.assertEqual(r.extra["artist"], want, f"data={data!r}")

    def test_artist_url_from_member(self):
        p = _ok_payload()
        p["results"][0]["data"] = {"member": "555"}
        r = parse_saucenao_response(p).results[0]
        self.assertEqual(r.extra["artist_url"], "https://www.pixiv.net/users/555")

    # ---- 命中库 ----
    def test_library_from_index_name_and_id(self):
        p = _ok_payload()
        p["results"][0]["header"] = {"similarity": "90", "index_id": 5}
        r = parse_saucenao_response(p).results[0]
        self.assertEqual(r.extra["library"], "pixiv")
        p2 = _ok_payload()
        p2["results"][0]["header"] = {"similarity": "90", "index_id": 999, "index_name": "custom"}
        r2 = parse_saucenao_response(p2).results[0]
        self.assertEqual(r2.extra["library"], "custom")
        p3 = _ok_payload()
        p3["results"][0]["header"] = {"similarity": "90", "index_id": 999}
        r3 = parse_saucenao_response(p3).results[0]
        self.assertEqual(r3.extra["library"], "库#999")

    def test_thumbnail_signed_not_leaked_into_title(self):
        r = parse_saucenao_response(_ok_payload()).results[0]
        self.assertIsNotNone(r.thumbnail)
        self.assertNotIn("auth=", r.title or "")

    def test_non_dict_payload(self):
        for bad in ([], "x", 5, None):
            out = parse_saucenao_response(bad)
            self.assertEqual(out.results, [])
            self.assertTrue(out.warnings)

    def test_sort_desc_and_filter(self):
        p = {"header": {"status": 0}, "results": [
            {"header": {"similarity": "10"}, "data": {"title": "c"}},
            {"header": {"similarity": "90"}, "data": {"title": "a"}},
            {"header": {"similarity": "70"}, "data": {"title": "b"}},
        ]}
        out = parse_saucenao_response(p, min_similarity=50)
        self.assertEqual([r.score for r in out.results], [90.0, 70.0])


# ===========================================================================
# D. 掩码与配置
# ===========================================================================
class TestMaskAndConfig(unittest.TestCase):
    def test_mask_96_independently(self):
        # 独立计算：pixiv=0x20, pixivhistorical=0x40
        self.assertEqual(0x20, 32)
        self.assertEqual(0x40, 64)
        self.assertEqual(0x20 | 0x40, 96)
        self.assertEqual(DEFAULT_DB_MASK, 96)

    def test_mask_zero_means_all(self):
        self.assertEqual(resolve_db_mask(0), 0)
        self.assertIn("全部库", describe_db_mask(0))

    def test_mask_invalid_variants(self):
        for bad in (-1, -999, None, "abc", "", True, False, 96.5, {}, [], "0x60x"):
            self.assertEqual(resolve_db_mask(bad), 96, f"{bad!r} 应回退 96")
        # 合法十六进制字符串
        self.assertEqual(resolve_db_mask("0x60"), 96)
        self.assertEqual(resolve_db_mask("96"), 96)
        self.assertEqual(resolve_db_mask(96.0), 96)

    def test_min_similarity_bounds(self):
        for bad in (-1, 101, 999, -5, None, "abc", True, 50.5):
            self.assertEqual(resolve_min_similarity(bad), 50, f"{bad!r} 应回退 50")
        for ok in (0, 50, 100):
            self.assertEqual(resolve_min_similarity(ok), ok)

    def test_hide_bounds(self):
        for bad in (-1, 4, 99, None, "x", True, 1.5):
            self.assertEqual(resolve_hide(bad), 0, f"{bad!r} 应回退 0")
        for ok in (0, 1, 2, 3):
            self.assertEqual(resolve_hide(ok), ok)

    def test_plugin_config_24_keys(self):  # [工程师已改 #6] 新增 4 项，20 -> 24
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(len(schema), 24)
        for k in ("saucenao_api_key", "saucenao_base_url", "saucenao_db_mask",
                  "saucenao_min_similarity", "saucenao_hide",
                  "saucenao_enable", "ascii2d_enable", "ascii2d_base_url", "ascii2d_bovw"):
            self.assertIn(k, schema)

    def test_plugin_defaults_and_override(self):
        p = SoutuSearchPlugin(object(), {})
        self.assertEqual(p.saucenao_db_mask, 96)
        self.assertEqual(p.saucenao_min_similarity, 50)
        self.assertEqual(p.saucenao_hide, 0)
        self.assertEqual(p.saucenao.base_url, "https://saucenao.com")
        p2 = SoutuSearchPlugin(object(), {"saucenao_base_url": "https://m.example.com"})
        self.assertEqual(p2.saucenao.base_url, "https://m.example.com")


# ===========================================================================
# E. 配额与 api_key 引导
# ===========================================================================
class TestQuotaAndKey(unittest.TestCase):
    def test_long_remaining_zero_warns(self):
        p = _ok_payload(header={"long_remaining": 0})
        out = parse_saucenao_response(p)
        self.assertTrue(any("配额" in w for w in out.warnings))

    def test_short_remaining_zero_warns(self):
        p = _ok_payload(header={"short_remaining": 0})
        out = parse_saucenao_response(p)
        self.assertTrue(any("限流" in w for w in out.warnings))

    def test_quota_missing_no_crash(self):
        p = _ok_payload()
        del p["header"]["long_remaining"]
        del p["header"]["short_remaining"]
        out = parse_saucenao_response(p)
        self.assertIsNone(out.meta["quota"]["long_remaining"])
        self.assertFalse(any("配额" in w or "限流" in w for w in out.warnings))

    def test_quota_warning_surfaces_in_formatted_output(self):
        # 配额耗尽 + 无结果时，用户必须看到配额提示（而非「搜不到图」）
        p = _ok_payload(header={"long_remaining": 0, "short_remaining": 0})
        p["results"] = []
        out = parse_saucenao_response(p)
        blocks = format_outcome(out, nsfw_send_image=False, max_results=3, header="H")
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertIn("配额", text)

    def test_status_error_raises_runtimeerror(self):
        c = SaucenaoClient(api_key="K")
        c._session = RecordingSession(RecordingResp(200, json.dumps({"header": {"status": 2}, "results": []})))
        with self.assertRaises(RuntimeError) as ctx:
            run(c.search(b"\xff\xd8\xff"))
        self.assertIn("状态码 2", str(ctx.exception))


class _FakeEvent:
    def __init__(self, umo="umo-A", text=""):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = type("M", (), {"message_id": "m1"})()
        self._umo = umo

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, comps):
        return ("chain", comps)


class TestApiKeyMissingNoRequest(unittest.TestCase):
    def test_skip_saucenao_but_ascii2d_runs_when_key_missing(self):
        """未配置 api_key：不发 SauceNAO 请求，但 ascii2d 仍运行。 [工程师已改 #7]"""
        p = SoutuSearchPlugin(object(), {})  # 无 key
        spy = RecordingSession()
        p.saucenao._session = spy
        a2d = _StubAscii2dRecord()
        p.ascii2d = a2d  # type: ignore[assignment]

        async def fe(event):
            return ImagePayload(data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, mime="image/png", filename="q.png")

        p.image_source.from_event = fe  # type: ignore[assignment]
        p.image_source.has_image = lambda ev: True  # type: ignore[assignment]
        out = collect(p.sou_cmd(_FakeEvent()))
        self.assertEqual(len(spy.calls), 0, "未配置 api_key 时绝不能发出 SauceNAO 网络请求")
        self.assertEqual(len(a2d.calls), 1, "未配置 key 时 ascii2d 仍应运行")
        text = _text_of(out[0])
        self.assertIn("跳过", text)
        self.assertIn("ascii2d", text)


# ===========================================================================
# F. 指令行为 / 访问控制
# ===========================================================================
class TestCommandBehaviour(unittest.TestCase):
    def test_commands_recognized(self):
        for t in ("/搜图", "搜图", "#搜图", "/pixiv", "/saucenao",
                  "/搜图帮助", "/搜图 猫娘", "/pixiv 猫娘", "#pixiv",
                  "/pixivhelp", "/saucenaohelp"):
            self.assertIsNotNone(_command_head(t), f"应识别: {t!r}")

    def test_removed_pixiv_cmd_not_command(self):
        """0.5.0 起 ``搜P站`` / ``搜P站帮助`` 已移除。"""
        for t in ("/搜P站", "搜P站", "#搜P站", "/搜P站帮助", "搜P站帮助x"):
            self.assertIsNone(_command_head(t), f"不应识别为指令: {t!r}")

    def test_human_text_not_command(self):
        for t in ("搜图真有意思", "pixiv真好用", "pixiv站", "搜图真有意思啊",
                  "saucenao很好用", "saucenao不错", "pixiv很好用", "soutubot很棒", "找图…"):
            self.assertIsNone(_command_head(t), f"不应识别为指令: {t!r}")

    def test_help_names_longest_first(self):
        self.assertEqual(_command_head("/搜图帮助"), "搜图帮助")
        self.assertEqual(_command_head("/搜图"), "搜图")
        self.assertEqual(_command_head("/pixivhelp"), "pixivhelp")
        self.assertEqual(_command_head("/pixiv"), "pixiv")
        self.assertEqual(_command_head("/saucenaohelp"), "saucenaohelp")
        self.assertEqual(_command_head("/saucenao"), "saucenao")

    def test_access_mode_consistency_sou_vs_book(self):
        cfg = {"access_mode": "whitelist", "whitelist": [], "saucenao_api_key": "k"}
        p = SoutuSearchPlugin(object(), cfg)
        # whitelist 空 → fail-closed，两条指令都应被拒
        self.assertEqual(collect(p.sou_cmd(_FakeEvent()))[0][1], ACCESS_DENIED_TEXT)
        self.assertEqual(collect(p.sou_help_cmd(_FakeEvent()))[0][1], ACCESS_DENIED_TEXT)
        self.assertEqual(collect(p.book_cmd(_FakeEvent()))[0][1], ACCESS_DENIED_TEXT)
        self.assertEqual(collect(p.book_help_cmd(_FakeEvent()))[0][1], ACCESS_DENIED_TEXT)

    def test_access_mode_blacklist_blocks_sou(self):
        cfg = {"access_mode": "blacklist", "blacklist": ["umo-A"], "saucenao_api_key": "k"}
        p = SoutuSearchPlugin(object(), cfg)
        self.assertEqual(collect(p.sou_cmd(_FakeEvent(umo="umo-A")))[0][1], ACCESS_DENIED_TEXT)

    def test_sou_no_image_no_args_gives_help(self):
        p = SoutuSearchPlugin(object(), {"saucenao_api_key": "k"})
        out = collect(p.sou_cmd(_FakeEvent()))
        self.assertEqual(out[0][1], HELP_TEXT)


# ===========================================================================
# G. 保留能力回归
# ===========================================================================
class TestRegression(unittest.TestCase):
    def test_formatter_no_artist_unchanged(self):
        """无 artist 字段时，结果行不应出现「画师」字样（与旧格式一致）。"""
        out = SourceOutcome(results=[
            SearchResult(title="作品", source="[soutubot]", url="https://x/y", score=88.0)
        ])
        blocks = format_outcome(out, nsfw_send_image=False, max_results=3, header="H")
        text = "\n".join(b.get("text", "") for b in blocks)
        self.assertNotIn("画师", text)
        self.assertIn("相似度 88.0%", text)
        self.assertIn("🔗 https://x/y", text)

    def test_formatter_artist_still_rendered(self):
        out = SourceOutcome(results=[
            SearchResult(title="作品", source="[pixiv]", url="https://x/y", score=88.0,
                         extra={"artist": "画师Z"})
        ])
        text = "\n".join(b.get("text", "") for b in format_outcome(
            out, nsfw_send_image=False, max_results=3, header="H"))
        self.assertIn("画师 画师Z", text)

    def test_nsfw_off_no_image_blocks_for_saucenao(self):
        r = parse_saucenao_response(_ok_payload()).results[0]
        out = SourceOutcome(results=[r], warnings=[])
        blocks = format_outcome(out, nsfw_send_image=False, max_results=3, header="H")
        self.assertFalse(any(b["type"] == "image" for b in blocks))
        joined = "\n".join(b.get("text", "") for b in blocks)
        self.assertNotIn("auth=", joined)

    def test_cache_keys_namespaced_distinct(self):
        data = b"same-image-bytes"
        img_key = cache_mod.make_image_key(data)
        snao_key = cache_mod.make_saucenao_key(data)
        self.assertNotEqual(img_key, snao_key, "soutubot 与 SauceNAO 缓存键必须不同")
        self.assertTrue(snao_key.startswith("snao:"))
        self.assertTrue(img_key.startswith("img:"))
        # 同一张图，两个源的缓存互不命中
        c = cache_mod.TTLCache(default_ttl=3600)
        c.set(img_key, "from-soutu")
        self.assertIsNone(c.get(snao_key))
        c.set(snao_key, "from-saucenao")
        self.assertEqual(c.get(img_key), "from-soutu")
        self.assertEqual(c.get(snao_key), "from-saucenao")

    def test_terminate_closes_saucenao(self):
        p = SoutuSearchPlugin(object(), {})
        closed = {"saucenao": False, "soutu": False, "booru": False, "image_source": False}

        class Stub:
            def __init__(self, key):
                self.key = key
            async def close(self):
                closed[self.key] = True

        p.saucenao = Stub("saucenao")
        p.soutu = Stub("soutu")
        p.booru = Stub("booru")
        p.image_source = Stub("image_source")
        run(p.terminate())
        self.assertTrue(all(closed.values()), f"terminate 应关闭全部会话: {closed}")

    def test_no_requests_import_anywhere(self):
        import re
        pattern = re.compile(r"(?m)^\s*(?:import\s+requests\b|from\s+requests\b)")
        for path in PLUGIN_ROOT.rglob("*.py"):
            src = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(pattern.search(src), f"{path} 不应 import requests")


if __name__ == "__main__":
    unittest.main(verbosity=2)
