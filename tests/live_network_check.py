"""真人端到端联网验证（不属于常规单测，需真实网络）。

直接调用插件自身的 core 客户端访问线上接口，验证：
1. multipart 字段名是否为 file/factor/metadata_mode/top_k
2. 请求头是否含已废弃的 X-Api-Key（不应含）
3. 真实响应能否被插件解析逻辑正确处理
4. Safebooru 关键词检索（blue_archive）解析是否正确

用法::
    python tests/live_network_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用 test_core 里的 astrbot 桩（导入即完成 sys.modules 装配）
import tests.test_core as _stub  # noqa: E402,F401

from astrbot_plugin_soutu_search.core.soutu_client import (  # noqa: E402
    SoutuClient,
    parse_soutu_response,
)
from astrbot_plugin_soutu_search.core.safebooru_client import (  # noqa: E402
    SafebooruClient,
    parse_safebooru_response,
)

TEST_IMG = PLUGIN_ROOT.parent / "recon" / "test.jpg"
CAPTURED: dict = {}


async def probe_soutu() -> None:
    print("=" * 78)
    print("[A] soutubot.moe 真实联网验证")
    print("=" * 78)
    client = SoutuClient(base_url="https://soutubot.moe", factor="1.2", timeout=60, min_score=0)
    image = TEST_IMG.read_bytes()
    print(f"输入图: {TEST_IMG} ({len(image)} bytes)")

    # --- 拦截 session.post，记录真实发出的 headers 与表单字段，再转发 ---
    real_get_session = client._get_session

    async def patched_get_session():
        session = await real_get_session()
        if getattr(session, "_probe_patched", False):
            return session
        real_post = session.post

        def spy_post(url, **kwargs):
            CAPTURED["headers"] = dict(kwargs.get("headers") or {})
            form = kwargs.get("data")
            fields = []
            try:
                for f in getattr(form, "_fields", []):
                    name = f.get("name")
                    val = f.get("value")
                    if hasattr(val, "read") or isinstance(val, (bytes, bytearray)):
                        fields.append((name, f"<binary {len(val) if hasattr(val,'__len__') else '?'}B>"))
                    else:
                        fields.append((name, repr(val)))
            except Exception as exc:  # noqa: BLE001
                fields.append(("<introspect-error>", repr(exc)))
            CAPTURED["fields"] = fields
            CAPTURED["url"] = url
            return real_post(url, **kwargs)

        session.post = spy_post  # type: ignore[assignment]
        session._probe_patched = True
        return session

    client._get_session = patched_get_session  # type: ignore[assignment]

    try:
        outcome = await client.search(image, filename="test.jpg", mime="image/jpeg", factor="1.2", top_k=25)
    finally:
        await client.close()

    print("\n--- 实际发出的请求 ---")
    print("POST", CAPTURED.get("url"))
    print("headers:", json.dumps(CAPTURED.get("headers", {}), ensure_ascii=False, indent=2))
    print("form fields:", CAPTURED.get("fields"))
    print("含 X-Api-Key ?", "X-Api-Key" in {k.lower(): v for k, v in CAPTURED.get("headers", {}).items()})

    print("\n--- 解析结果 ---")
    print("warnings:", outcome.warnings)
    print("meta:", outcome.meta)
    print("results:", len(outcome.results))
    for r in outcome.results[:5]:
        print(f"  - [{r.source}] {r.title[:40]!r} score={r.score} url={r.url}")


async def probe_safebooru() -> None:
    print("\n" + "=" * 78)
    print("[B] safebooru.org 真实联网验证 (tags=blue_archive)")
    print("=" * 78)
    client = SafebooruClient(base_url="https://safebooru.org", timeout=60, rating="safe")
    try:
        outcome = await client.search_by_tags("blue_archive", limit=5, page=0)
    finally:
        await client.close()
    print("warnings:", outcome.warnings)
    print("meta:", outcome.meta)
    print("results:", len(outcome.results))
    for r in outcome.results[:5]:
        print(f"  - [{r.source}] {r.title[:60]!r} score={r.score} thumb={r.thumbnail}")
        print(f"      url={r.url}  rating={r.extra.get('rating')}")


async def main() -> None:
    try:
        await probe_soutu()
    except Exception as exc:  # noqa: BLE001
        print(f"!!! soutubot 联网失败: {type(exc).__name__}: {exc}")
    try:
        await probe_safebooru()
    except Exception as exc:  # noqa: BLE001
        print(f"!!! safebooru 联网失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
