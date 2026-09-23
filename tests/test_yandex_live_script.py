"""``tests/live_yandex_check.py`` 的纯函数单元测试（离线，不发网络请求）。

覆盖：
- ``coerce_path``：只接受 ``str`` / ``pathlib.PurePath``；**拒绝 MagicMock 等伪路径**
  （本项目修过的坑：``isinstance(MagicMock(), os.PathLike)`` 为 True，若不加区分
  会让测试桩把垃圾目录写进仓库）；
- ``_build_body``：multipart 必须含 ``prg`` + ``upfile`` 字段，且产物为 **bytes**
  （保证 Content-Length，避免 Yandex 413）；
- ``--self-test`` 离线自检通过（不联网）。

运行::
    python -m unittest tests.test_yandex_live_script -v
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.live_yandex_check as live  # noqa: E402  (导入即装配 astrbot 桩)


class TestCoercePath(unittest.TestCase):
    def test_str_and_purepath_accepted(self):
        self.assertEqual(live.coerce_path("recon/x.html"), Path("recon/x.html"))
        self.assertEqual(live.coerce_path(Path("a/b.html")), Path("a/b.html"))

    def test_magicmock_rejected(self):
        """伪路径（MagicMock 满足 os.PathLike）必须被拒绝，避免把垃圾目录写进仓库。"""
        self.assertIsNone(live.coerce_path(MagicMock()))

    def test_none_and_blank_rejected(self):
        self.assertIsNone(live.coerce_path(None))
        self.assertIsNone(live.coerce_path(""))
        self.assertIsNone(live.coerce_path("   "))
        self.assertIsNone(live.coerce_path(123))


class TestBuildBody(unittest.TestCase):
    def test_contains_prg_and_upfile(self):
        body = live._build_body(b"\x89PNG\r\n\x1a\nIMG", "q.png", "image/png")
        text = body.decode("utf-8", errors="replace")
        self.assertIn('name="prg"', text)
        self.assertIn('name="upfile"', text)
        self.assertIn('filename="q.png"', text)
        self.assertIn("Content-Type: image/png", text)

    def test_body_is_bytes(self):
        """body 必须是 bytes（保证 Content-Length，避免 Yandex 413）。"""
        body = live._build_body(b"IMGDATA")
        self.assertIsInstance(body, bytes)
        self.assertIn(b"IMGDATA", body)

    def test_terminator_present(self):
        body = live._build_body(b"x")
        self.assertTrue(body.rstrip(b"\r\n").endswith(b"--"))


class TestSelfTestOffline(unittest.TestCase):
    def test_self_test_runs_without_network_and_passes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = live.self_test("https://yandex.ru")
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("自检通过", out)
        self.assertIn("https://yandex.ru/images/search", out)


if __name__ == "__main__":
    unittest.main()
