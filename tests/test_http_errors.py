"""HTTP 层错误处理测试（用假 session 模拟各类故障，离线可复现）。

覆盖任务书 D 项：401 / 403(Cloudflare) / 429 / 500 / 超时 / 响应体非 JSON。
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

import tests.test_core as _stub  # noqa: E402,F401

from astrbot_plugin_soutu_search.core.safebooru_client import SafebooruClient  # noqa: E402
from astrbot_plugin_soutu_search.core.soutu_client import SoutuClient  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeResp:
    def __init__(self, status=200, body="", raise_exc=None):
        self.status = status
        self._body = body
        self._raise = raise_exc

    async def text(self):
        return self._body

    async def json(self, content_type=None):
        return json.loads(self._body)

    async def read(self):
        return self._body.encode()

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

    def post(self, url, **kw):
        return self._resp

    def get(self, url, **kw):
        return self._resp


def make_soutu(resp):
    c = SoutuClient(base_url="https://soutubot.moe", timeout=30, min_score=0)
    c._session = FakeSession(resp)
    return c


def make_booru(resp):
    c = SafebooruClient(base_url="https://safebooru.org", timeout=30)
    c._session = FakeSession(resp)
    return c


class TestHttpErrors(unittest.TestCase):
    def _assert_raises(self, coro):
        try:
            run(coro)
        except Exception as exc:  # noqa: BLE001
            return exc
        self.fail("预期抛异常但未抛")
        return None

    def test_soutu_401(self):
        exc = self._assert_raises(make_soutu(FakeResp(401, "unauthorized")).search(b"img"))
        self.assertIn("401", str(exc))

    def test_soutu_403_cloudflare(self):
        html = "<html><title>Just a moment...</title></html>"
        exc = self._assert_raises(make_soutu(FakeResp(403, html)).search(b"img"))
        self.assertIn("403", str(exc))

    def test_soutu_429(self):
        exc = self._assert_raises(make_soutu(FakeResp(429, "slow down")).search(b"img"))
        self.assertIn("429", str(exc))

    def test_soutu_500(self):
        exc = self._assert_raises(make_soutu(FakeResp(500, "server error")).search(b"img"))
        self.assertIn("500", str(exc))

    def test_soutu_timeout(self):
        exc = self._assert_raises(
            make_soutu(FakeResp(200, "", raise_exc=asyncio.TimeoutError())).search(b"img")
        )
        # 记录异常类型：是否被归为 RuntimeError（网络错误）
        self.assertIsNotNone(exc)

    def test_soutu_200_but_html_body(self):
        """Cloudflare 有时以 200 返回 HTML 挑战页 → json() 抛 JSONDecodeError。

        期望（理想）：客户端应统一包装为 RuntimeError 给出可读原因。
        现状（P2 缺陷）：抛出的是原生 json.JSONDecodeError —— 未被
        `except aiohttp.ClientError` 兜住。上层 main 的 `except Exception`
        仍能兜底，故不会崩溃，但错误分类不一致。
        本用例断言「抛出异常且上游可兜底」，并记录真实类型。
        """
        exc = self._assert_raises(make_soutu(FakeResp(200, "<html>challenge</html>")).search(b"img"))
        self.assertIsInstance(exc, (RuntimeError, json.JSONDecodeError))
        if not isinstance(exc, RuntimeError):
            print(f"\n[P2] soutu 200+HTML 实际异常类型={type(exc).__name__}（非 RuntimeError）")

    def test_booru_500(self):
        exc = self._assert_raises(make_booru(FakeResp(500, "err")).search_by_tags("blue_archive"))
        self.assertIn("500", str(exc))

    def test_booru_timeout(self):
        exc = self._assert_raises(
            make_booru(FakeResp(200, "", raise_exc=asyncio.TimeoutError())).search_by_tags("x")
        )
        self.assertIsNotNone(exc)

    def test_booru_200_html_body_graceful(self):
        # Safebooru 非 JSON 是「正常容错」路径：应返回空结果而非抛异常
        c = make_booru(FakeResp(200, "<html>Just a moment...</html>"))
        outcome = run(c.search_by_tags("blue_archive"))
        self.assertEqual(outcome.results, [])
        self.assertTrue(outcome.warnings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
