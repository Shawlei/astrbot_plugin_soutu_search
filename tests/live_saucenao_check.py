"""SauceNAO 真人端到端联网校验（**不属于常规单测，需真实网络**）。

⚠️ 开发机（本仓库所在环境）**连不通 saucenao.com（TLS 连接重置）**，因此本脚本是
给**用户在自己机器上**（很可能挂了代理）复验用的。它会：

1. **连通性检查**：GET ``<base_url>/`` 打印 HTTP 状态码（连不通时给出可读提示）；
2. **真实反查**：用插件自身的 ``SaucenaoClient`` 上传图片调用 ``/search.php``，
   打印解析出的结果条数、首条的相似度 / 标题 / 链接 / 画师，以及 ``header`` 里的配额字段
   （``short_remaining`` / ``long_remaining`` / ``short_limit`` / ``long_limit``）；
3. **请求自检**：打印实际发出的 multipart 表单字段名（确认是 ``file``）与查询参数
   （**api_key 会脱敏为前 3 位 + `***`**，不会泄漏完整密钥）。

用法::

    # 最简：用环境变量提供 key 与（可选）图片
    set SAUCENAO_API_KEY=你的key        # Windows cmd
    export SAUCENAO_API_KEY=你的key     # bash
    python tests/live_saucenao_check.py

    # 或用参数
    python tests/live_saucenao_check.py --api-key 你的key --image path/to/pic.jpg
    python tests/live_saucenao_check.py --base-url https://你的反代/  # 应对网络问题

    # 离线自检请求构造（不发网络请求，专门核对 multipart 字段名与参数脱敏）
    python tests/live_saucenao_check.py --self-test --api-key 你的key

说明：
- AstrBot 有全局 ``http_proxy`` 配置；本脚本会用系统 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量。
- 免费账户配额：150 次/天、4 次/30 秒。切勿循环调用。
- 网络反查失败时**仍会打印 ``[3/3]`` 请求自检**（请求已构造/发出），便于核对未实测的字段名 ``file``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用 test_core 里的 astrbot 桩（导入即完成 sys.modules 装配），使脚本可在无 AstrBot 环境运行
import tests.test_core as _stub  # noqa: E402,F401

import aiohttp  # noqa: E402

from astrbot_plugin_soutu_search.core.saucenao_client import (  # noqa: E402
    DEFAULT_DB_MASK,
    describe_db_mask,
    SaucenaoClient,
)

DEFAULT_IMAGE = PLUGIN_ROOT.parent / "recon" / "test.jpg"


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


async def check_reachability(base_url: str, timeout: int = 15) -> bool:
    """检查 ``<base_url>/`` 是否可达；打印 HTTP 状态码或可读错误。返回是否成功。"""
    _hr(f"[1/3] 连通性检查: GET {base_url}/")
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=float(timeout))
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(f"{base_url}/") as resp:
                print(f"HTTP 状态码: {resp.status}")
                ok = resp.status < 500
                print("结论:", "可连通 ✅" if ok else "服务端异常 ❌")
                return ok
    except asyncio.TimeoutError:
        print("结论: 连接超时 ❌ —— 大陆通常需要代理（设置 HTTPS_PROXY 或使用 --base-url 指定反代）")
    except aiohttp.ClientConnectorCertificateError as exc:
        print(f"结论: TLS 证书错误 ❌（{exc}）")
    except aiohttp.ClientConnectorError as exc:
        print(f"结论: 无法连接 ❌（{exc}）—— 大陆通常需要代理")
    except aiohttp.ClientError as exc:
        print(f"结论: 网络错误 ❌（{type(exc).__name__}: {exc}）")
    return False


# --------------------------------------------------------------------------- #
# 纯函数：脱敏 与 表单内省（抽成纯函数，便于单测）
# --------------------------------------------------------------------------- #
# 需要脱敏的查询参数名（小写比较）
_SECRET_PARAM_KEYS = {"api_key", "apikey", "key", "token", "secret"}


def redact_secret(value, *, keep_head: int = 3) -> str:
    """把密钥脱敏为「前 keep_head 位 + ``***``」；过短则整体 ``***``（绝不泄漏完整密钥）。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if len(text) > keep_head:
        return text[:keep_head] + "***"
    return "***"


def redact_params(params) -> dict:
    """复制查询参数，并在打印前对敏感字段（如 ``api_key``）脱敏。"""
    out: dict = {}
    for key, value in dict(params or {}).items():
        if str(key).lower() in _SECRET_PARAM_KEYS:
            out[key] = redact_secret(value)
        else:
            out[key] = value
    return out


def format_field_value(value) -> str:
    """把表单字段值渲染为可读字符串（二进制只显示字节数，**绝不**打印内容）。"""
    if isinstance(value, (bytes, bytearray)):
        return f"<binary {len(value)}B>"
    return repr(value)


def extract_form_fields(form):
    """稳健抽取 ``aiohttp.FormData`` 的 ``(字段名, 值)`` 列表，返回 ``(fields, ok)``。

    ``ok=False`` 表示**无法内省**（例如 aiohttp 版本差异导致 ``_fields`` 结构不认识）——
    这与「字段名缺失」是**两回事**，调用方必须区分对待，不能把内省失败误报成字段名错误。

    兼容多种 ``_fields`` 元素形态：
    - ``dict``（含 ``name`` / ``value``）；
    - **元组** ``(info, headers, value)``（aiohttp 3.14 实测形态，``info`` 可取 ``name``）；
    - 带 ``.get`` 的对象（较老版本）。
    """
    entries = getattr(form, "_fields", None)
    if entries is None:
        return [], False
    fields: list[tuple[str, object]] = []
    try:
        for entry in entries:
            name = None
            value = None
            if isinstance(entry, dict):
                name, value = entry.get("name"), entry.get("value")
            elif isinstance(entry, tuple):
                info = entry[0]
                if hasattr(info, "get"):
                    name = info.get("name")
                elif isinstance(info, (tuple, list)) and info:
                    name = info[0]
                value = entry[-1]
            elif hasattr(entry, "get"):
                name, value = entry.get("name"), entry.get("value")
            else:
                return [], False
            if name is None:
                return [], False
            fields.append((str(name), value))
        return fields, True
    except Exception:  # noqa: BLE001 - 任何结构异常都视为「无法内省」
        return [], False


def _spy_form_fields(client: SaucenaoClient) -> dict:
    """挂一个请求拦截器，记录实际发出的表单字段（名）+ 查询参数，便于核对 multipart 字段名。"""
    captured: dict = {}
    real_get_session = client._get_session

    async def patched_get_session():
        session = await real_get_session()
        if getattr(session, "_probe_patched", False):
            return session
        real_post = session.post

        def spy_post(url, **kwargs):
            captured["url"] = url
            captured["params"] = dict(kwargs.get("params") or {})
            captured["headers"] = dict(kwargs.get("headers") or {})
            fields, ok = extract_form_fields(kwargs.get("data"))
            captured["fields_ok"] = ok
            captured["field_names"] = [name for name, _ in fields]
            captured["fields"] = [(name, format_field_value(value)) for name, value in fields]
            return real_post(url, **kwargs)

        session.post = spy_post  # type: ignore[assignment]
        session._probe_patched = True
        return session

    client._get_session = patched_get_session  # type: ignore[assignment]
    return captured


def _print_request_selfcheck(captured: dict) -> None:
    """打印 ``[3/3]`` 请求自检块。

    - 查询参数打印前经 :func:`redact_params` 脱敏（``api_key`` 只显前 3 位 + ``***``）；
    - **严格区分**「无法内省 FormData」（``fields_ok=False``）与「字段名不是 file」——
      前者是 aiohttp 版本差异，**不能**误导用户以为字段名错了。
    """
    _hr("[3/3] 请求自检（确认 multipart 字段名 / 查询参数）")
    print("URL       :", captured.get("url"))
    print("查询参数  :", json.dumps(redact_params(captured.get("params", {})), ensure_ascii=False),
          "（api_key 已脱敏，仅显示前 3 位）")
    print("表单字段  :", captured.get("fields"))
    print("Referer   :", captured.get("headers", {}).get("Referer"))
    if captured.get("fields_ok"):
        names = captured.get("field_names", [])
        if "file" in names:
            print("含 file 字段 ? True（正确）")
        else:
            print(f"含 file 字段 ? False（!!! 异常：字段名为 {names!r}，请把本段输出贴给开发者）")
    else:
        print("含 file 字段 ? 无法内省 FormData（**不是**字段名错误；可能是 aiohttp 版本差异）")


async def do_search(
    *,
    base_url: str,
    api_key: str,
    image_path: Path,
    db_mask: int,
    min_similarity: int,
    timeout: int,
) -> None:
    _hr(f"[2/3] 真实反查: POST {base_url}/search.php  (dbmask={db_mask} {describe_db_mask(db_mask)})")

    if not image_path.exists():
        print(f"!! 找不到测试图片: {image_path}（可用 --image 指定）")
        return
    image = image_path.read_bytes()
    print(f"输入图: {image_path} ({len(image)} bytes)")
    print(f"API Key: {'已提供' if api_key else '未提供（SauceNAO 无 key 功能受限、限流更严）'}")

    client = SaucenaoClient(
        base_url=base_url,
        api_key=api_key,
        db_mask=db_mask,
        min_similarity=min_similarity,
        timeout=timeout,
    )
    captured = _spy_form_fields(client)

    outcome = None
    error: Exception | None = None
    try:
        outcome = await client.search(image, filename=image_path.name, mime="image/jpeg")
    except Exception as exc:  # noqa: BLE001
        error = exc
    finally:
        await client.close()

    # 无论成功与否都打印请求自检——请求已构造/发出，这正是核对 multipart 字段名的关键
    _print_request_selfcheck(captured)

    if error is not None:
        print(f"\n!! 反查失败: {type(error).__name__}: {error}")
        print("   排查建议：① 确认已配置代理；② 用 --base-url 指定可用的镜像/反代；")
        print("             ③ 确认 API Key 正确；④ 若为 403/非 JSON，可能被 Cloudflare 拦截。")
        print("   （字段名自检不受网络影响，见上方 [3/3]）")
        return

    _hr("解析结果")
    print(f"结果条数: {len(outcome.results)}")
    print("warnings :", outcome.warnings)
    quota = outcome.meta.get("quota") or {}
    print("配额(header):", json.dumps(quota, ensure_ascii=False))
    if quota.get("long_remaining") is not None:
        print(f"  · 24 小时剩余 {quota.get('long_remaining')} / 上限 {quota.get('long_limit')}")
    if quota.get("short_remaining") is not None:
        print(f"  · 30 秒剩余  {quota.get('short_remaining')} / 上限 {quota.get('short_limit')}")

    if not outcome.results:
        print("（无结果：可能确实无匹配，或命中的库不在 dbmask 范围，或配额已耗尽）")
        return
    for i, result in enumerate(outcome.results[:5], start=1):
        print(f"\n{i}. [{result.source}] {result.title or '（无标题）'}")
        print(f"   相似度: {result.score}")
        print(f"   画师  : {result.extra.get('artist')}  {result.extra.get('artist_url') or ''}")
        print(f"   链接  : {result.url or '（无链接）'}")
        print(f"   缩略图: {'有（带签名，需先下载再发，勿直接外链）' if result.thumbnail else '无'}")


async def do_self_test(*, base_url: str, api_key: str, db_mask: int, min_similarity: int) -> None:
    """离线自检：**不发任何网络请求**，仅验证请求构造（字段名 / 参数 / 脱敏）。"""
    client = SaucenaoClient(base_url=base_url, api_key=api_key, db_mask=db_mask, min_similarity=min_similarity)
    form = client.build_form(b"\xff\xd8\xffSELFTEST", filename="selftest.jpg", mime="image/jpeg")
    fields, ok = extract_form_fields(form)
    captured = {
        "url": f"{base_url}/search.php",
        "params": client._build_params(),
        "headers": {"Referer": f"{base_url}/"},
        "fields_ok": ok,
        "field_names": [name for name, _ in fields],
        "fields": [(name, format_field_value(value)) for name, value in fields],
    }
    _print_request_selfcheck(captured)
    print("\n说明：本自检**不联网**，仅验证请求构造（multipart 字段名 / 查询参数 / 脱敏）。")
    print("      真实联网的解析结果，请在不带 --self-test 的情况下运行本脚本。")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SauceNAO 联网校验脚本")
    parser.add_argument("--api-key", default=os.environ.get("SAUCENAO_API_KEY", ""),
                        help="SauceNAO API Key（默认读环境变量 SAUCENAO_API_KEY）")
    parser.add_argument("--base-url", default=os.environ.get("SAUCENAO_BASE_URL", "https://saucenao.com"),
                        help="接口地址（可填镜像/反代；默认读 SAUCENAO_BASE_URL 或官方站点）")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE), help="待反查的图片路径")
    parser.add_argument("--db-mask", type=int, default=DEFAULT_DB_MASK,
                        help="数据库位掩码（默认 96 = 仅 Pixiv；0 = 不限库，将不发送该参数）")
    parser.add_argument("--min-similarity", type=int, default=50, help="最低相似度（0-100）")
    parser.add_argument("--timeout", type=int, default=60, help="请求超时（秒）")
    parser.add_argument("--skip-connectivity", action="store_true", help="跳过连通性检查")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检请求构造（不发网络请求）：验证 multipart 字段名与参数脱敏")
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()
    base_url = str(args.base_url).rstrip("/")

    if args.self_test:
        print("SauceNAO 请求构造离线自检（--self-test）")
        print(f"base_url = {base_url}")
        await do_self_test(
            base_url=base_url,
            api_key=args.api_key,
            db_mask=args.db_mask,
            min_similarity=args.min_similarity,
        )
        print("\n完成。")
        return

    print("SauceNAO 联网校验")
    print(f"base_url = {base_url}")
    print(f"image    = {args.image}")
    print("提示：免费账户配额 150 次/天、4 次/30 秒；大陆通常需要代理。")

    if not args.skip_connectivity:
        reachable = await check_reachability(base_url, timeout=min(args.timeout, 15))
        if not reachable:
            print("\n⚠️ 连通性检查未通过。仍会尝试直接反查（有时首页被墙但 search.php 可用）；")
            print("   若失败，请设置代理后重试，或用 --base-url 指定可用的镜像/反代。")

    await do_search(
        base_url=base_url,
        api_key=args.api_key,
        image_path=Path(args.image),
        db_mask=args.db_mask,
        min_similarity=args.min_similarity,
        timeout=args.timeout,
    )
    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
