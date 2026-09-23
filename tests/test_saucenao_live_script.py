"""`tests/live_saucenao_check.py` 的纯函数单元测试（QA P1 修复的回归护栏）。

覆盖：
- ``redact_secret`` / ``redact_params``：打印前对 ``api_key`` 脱敏，**绝不**泄漏完整密钥；
- ``extract_form_fields``：兼容 aiohttp 多种 ``_fields`` 形态（元组 / dict / 对象），
  且**内省失败**（``ok=False``）与「字段名缺失」严格区分；
- ``_spy_form_fields`` 端到端：真实 ``SaucenaoClient.search`` 走假 session，
  断言能读出 ``file`` 字段、且截获的 ``api_key`` 经 ``redact_params`` 后已脱敏；
- 源码护栏：脚本打印查询参数必须经过 ``redact_params``。

运行::
    python -m unittest tests.test_saucenao_live_script -v
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

import tests.live_saucenao_check as live  # noqa: E402  (导入即装配 astrbot 桩)
from astrbot_plugin_soutu_search.core.saucenao_client import SaucenaoClient  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ===========================================================================
# 1. 脱敏
# ===========================================================================
class TestRedaction(unittest.TestCase):
    def test_redact_long_keeps_head_only(self):
        self.assertEqual(live.redact_secret("SUPER_SECRET_KEY_12345"), "SUP***")

    def test_redact_short_is_all_masked(self):
        self.assertEqual(live.redact_secret("ab"), "***")
        self.assertEqual(live.redact_secret("abc"), "***")

    def test_redact_empty_and_none(self):
        self.assertEqual(live.redact_secret(""), "")
        self.assertEqual(live.redact_secret(None), "")

    def test_redact_params_masks_api_key_only(self):
        out = live.redact_params(
            {"output_type": "2", "dbmask": "96", "api_key": "SUPER_SECRET_KEY_12345"}
        )
        self.assertEqual(out["api_key"], "SUP***")
        self.assertEqual(out["dbmask"], "96")
        self.assertEqual(out["output_type"], "2")
        # 明文密钥绝不出现在序列化结果里
        self.assertNotIn("SECRET", json.dumps(out, ensure_ascii=False))

    def test_redact_params_other_secret_names(self):
        out = live.redact_params({"token": "abcdef", "Key": "xyz12345", "secret": "qwertyui"})
        self.assertEqual(out["token"], "abc***")
        self.assertEqual(out["Key"], "xyz***")
        self.assertEqual(out["secret"], "qwe***")

    def test_redact_params_does_not_mutate_input(self):
        params = {"api_key": "SECRETKEY"}
        live.redact_params(params)
        self.assertEqual(params["api_key"], "SECRETKEY", "脱敏不得修改原字典")

    def test_redact_params_handles_none(self):
        self.assertEqual(live.redact_params(None), {})
        self.assertEqual(live.redact_params({}), {})


# ===========================================================================
# 2. 表单内省（P1-2）
# ===========================================================================
class TestFormIntrospection(unittest.TestCase):
    def test_real_aiohttp_formdata_reads_file(self):
        """真实 aiohttp.FormData（3.14 为元组形态）必须能读出 `file` 字段。"""
        form = aiohttp.FormData()
        form.add_field("file", b"\xff\xd8\xffIMG", filename="q.jpg", content_type="image/jpeg")
        form.add_field("factor", "1.2")
        fields, ok = live.extract_form_fields(form)
        self.assertTrue(ok, "真实 FormData 内省必须成功")
        names = [n for n, _ in fields]
        self.assertEqual(names, ["file", "factor"])
        self.assertEqual(fields[0][1], b"\xff\xd8\xffIMG")
        self.assertEqual(fields[1][1], "1.2")

    def test_tuple_form_shape(self):
        class TupleForm:
            _fields = [
                ({"name": "file"}, {"Content-Type": "image/jpeg"}, b"xx"),
                ({"name": "factor"}, {}, "1.2"),
            ]

        fields, ok = live.extract_form_fields(TupleForm())
        self.assertTrue(ok)
        self.assertEqual([n for n, _ in fields], ["file", "factor"])
        self.assertEqual(fields[0][1], b"xx")

    def test_dict_form_shape(self):
        class DictForm:
            _fields = [
                {"name": "file", "value": b"xx"},
                {"name": "factor", "value": "1.2"},
            ]

        fields, ok = live.extract_form_fields(DictForm())
        self.assertTrue(ok)
        self.assertEqual([n for n, _ in fields], ["file", "factor"])

    def test_object_get_form_shape(self):
        class Entry:
            def get(self, key):
                return {"name": "file", "value": b"z"}.get(key)

        class ObjForm:
            _fields = [Entry()]

        fields, ok = live.extract_form_fields(ObjForm())
        self.assertTrue(ok)
        self.assertEqual(fields[0][0], "file")

    def test_unknown_shape_is_not_ok(self):
        class WeirdForm:
            _fields = [object(), 1, 2]

        fields, ok = live.extract_form_fields(WeirdForm())
        self.assertFalse(ok, "不认识的形态应返回 ok=False（而非误报字段缺失）")
        self.assertEqual(fields, [])

    def test_no_fields_attr_is_not_ok(self):
        class NoFields:
            pass

        fields, ok = live.extract_form_fields(NoFields())
        self.assertFalse(ok)

    def test_none_is_not_ok(self):
        fields, ok = live.extract_form_fields(None)
        self.assertFalse(ok)

    def test_introspect_failure_is_distinct_from_missing_field(self):
        """内省失败（ok=False）与「字段名缺失」（ok=True 但名字不含 file）必须可区分。"""

        class Weird:
            _fields = [1]

        _, ok_fail = live.extract_form_fields(Weird())

        class Missing:
            _fields = [{"name": "other", "value": b"x"}]

        fields_ok, ok_ok = live.extract_form_fields(Missing())
        self.assertFalse(ok_fail, "无法内省 → ok=False")
        self.assertTrue(ok_ok, "能内省 → ok=True")
        self.assertNotIn("file", [n for n, _ in fields_ok])

    def test_format_field_value_hides_binary_content(self):
        self.assertEqual(live.format_field_value(b"\xff\xd8\xffIMG"), "<binary 6B>")
        self.assertEqual(live.format_field_value("1.2"), "'1.2'")
        self.assertNotIn("\xff", live.format_field_value(b"\xff\xd8\xffIMG"))


# ===========================================================================
# 3. 端到端：假 session 驱动真实 search，验证拦截器读出 file 且 key 已脱敏
# ===========================================================================
class TestSpyIntegration(unittest.TestCase):
    class _Resp:
        status = 200

        async def text(self):
            return json.dumps({"header": {"status": 0}, "results": []})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Sess:
        closed = False

        def __init__(self):
            self.calls = []

        def post(self, url, **kw):
            self.calls.append(kw)
            return TestSpyIntegration._Resp()

    def test_spy_reads_file_field_and_masks_key(self):
        client = SaucenaoClient(api_key="SECRETKEY123", db_mask=96)
        captured = live._spy_form_fields(client)
        client._session = self._Sess()
        run(client.search(b"\xff\xd8\xffIMG", filename="q.jpg", mime="image/jpeg"))

        self.assertTrue(captured.get("fields_ok"), "内省应成功")
        self.assertIn("file", captured.get("field_names", []))
        # 参数里的 api_key 经脱敏后不含明文
        redacted = live.redact_params(captured.get("params", {}))
        self.assertEqual(redacted["api_key"], "SEC***")
        self.assertNotIn("SECRETKEY123", json.dumps(redacted, ensure_ascii=False))


# ===========================================================================
# 4. 源码护栏：脚本打印参数必须经过 redact_params
# ===========================================================================
class TestScriptSourceGuards(unittest.TestCase):
    def _src(self):
        return (PLUGIN_ROOT / "tests" / "live_saucenao_check.py").read_text(encoding="utf-8")

    def test_params_printed_via_redact(self):
        src = self._src()
        self.assertIn("redact_params(", src, "脚本必须用 redact_params 处理后再打印参数")

    def test_no_raw_params_dump(self):
        src = self._src()
        # 不得出现未脱敏地直接 dump captured params 的写法
        self.assertNotIn(
            'json.dumps(captured.get("params", {}), ensure_ascii=False)',
            src,
            "不得直接打印未脱敏的查询参数",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
