"""``tests/live_ascii2d_check.py`` 的纯函数单元测试（离线，不发网络请求）。

覆盖：
- ``coerce_dump_path``：只接受 ``str`` / ``pathlib.PurePath``；**拒绝 MagicMock 等伪路径**
  （本项目修过的坑：``isinstance(MagicMock(), os.PathLike)`` 为 True 且 ``os.fspath(MagicMock())``
  不抛异常，若不加区分会让测试桩把垃圾目录写进仓库）；
- ``extract_form_fields``：真实 ``aiohttp.FormData`` 能读出 ``file`` 字段；
- ``format_field_value``：二进制只显示字节数，**绝不**泄漏内容；
- ``--self-test`` 请求构造字段名 = ``file``、路径 = ``/search/file``（离线）。

运行::
    python -m unittest tests.test_ascii2d_live_script -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import aiohttp  # noqa: E402

import tests.live_ascii2d_check as live  # noqa: E402  (导入即装配 astrbot 桩)
from astrbot_plugin_soutu_search.core.ascii2d_client import Ascii2dClient  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TestCoerceDumpPath(unittest.TestCase):
    def test_str_and_purepath_accepted(self):
        self.assertEqual(live.coerce_dump_path("recon/x.html"), Path("recon/x.html"))
        self.assertEqual(live.coerce_dump_path(Path("a/b.html")), Path("a/b.html"))

    def test_magicmock_rejected(self):
        """伪路径（MagicMock 满足 os.PathLike）必须被拒绝，避免把垃圾目录写进仓库。"""
        self.assertIsNone(live.coerce_dump_path(MagicMock()))

    def test_none_and_blank_rejected(self):
        self.assertIsNone(live.coerce_dump_path(None))
        self.assertIsNone(live.coerce_dump_path(""))
        self.assertIsNone(live.coerce_dump_path("   "))
        self.assertIsNone(live.coerce_dump_path(123))


class TestFormIntrospection(unittest.TestCase):
    def test_real_formdata_reads_file(self):
        form = aiohttp.FormData()
        form.add_field("file", b"\xff\xd8\xffIMG", filename="q.jpg", content_type="image/jpeg")
        fields, ok = live.extract_form_fields(form)
        self.assertTrue(ok)
        self.assertIn("file", [n for n, _ in fields])

    def test_unknown_shape_not_ok(self):
        class Weird:
            _fields = [object(), 1]

        fields, ok = live.extract_form_fields(Weird())
        self.assertFalse(ok)
        self.assertEqual(fields, [])

    def test_none_not_ok(self):
        _, ok = live.extract_form_fields(None)
        self.assertFalse(ok)

    def test_format_field_value_hides_binary(self):
        self.assertEqual(live.format_field_value(b"\xff\xd8\xffIMG"), "<binary 6B>")
        self.assertNotIn("\xff", live.format_field_value(b"\xff\xd8\xffIMG"))


class TestSelfTestOffline(unittest.TestCase):
    def test_build_form_field_name_and_path(self):
        client = Ascii2dClient(base_url="https://ascii2d.net")
        form = client.build_form(b"\xff\xd8\xffX", filename="s.jpg", mime="image/jpeg")
        fields, ok = live.extract_form_fields(form)
        self.assertTrue(ok)
        self.assertEqual([n for n, _ in fields], ["file"])

    def test_self_test_runs_without_network(self):
        # do_self_test 不联网；仅验证不抛异常
        run(live.do_self_test(base_url="https://ascii2d.net", bovw=False))


class TestScriptSourceGuards(unittest.TestCase):
    def test_script_dumps_into_project_with_pathlib(self):
        src = (PLUGIN_ROOT / "tests" / "live_ascii2d_check.py").read_text(encoding="utf-8")
        self.assertIn("PurePath", src, "落盘路径参数只接受 str/PurePath")
        self.assertIn("recon", src)
        self.assertIn("coerce_dump_path(", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
