"""ascii2d 真人端到端联网校验（**不属于常规单测，需要真实网络**）。

⚠️ 开发机（本仓库所在环境）**curl 出口不可达 ascii2d.net**，因此本脚本是给
**用户在自己机器上**（很可能挂了代理）复验用的。它会：

1. **连通性检查**：GET ``<base_url>/`` 打印 HTTP 状态码（连不通时给出可读提示）；
2. **真实反查**：用插件自身的 ``Ascii2dClient`` 上传图片调用 ``/search/file``，
   打印解析出的结果条数、前 3 条的标题 / 来源 / 链接 / 画师，以及**原始响应 URL**
   （bovw 二次请求依赖该 URL 里的 ``/color/``）；
3. **请求自检**：打印实际发出的 multipart 字段名（确认是 ``file``）与请求头（无敏感信息）。

用法::

    # 最简（默认用 recon/test.jpg）
    python tests/live_ascii2d_check.py

    # 指定图片 / 镜像 / 特征检索 / 超时
    python tests/live_ascii2d_check.py --image path/to/pic.jpg
    python tests/live_ascii2d_check.py --base-url https://你的反代/
    python tests/live_ascii2d_check.py --bovw --timeout 90

    # 解析结果为 0 条时**自动**落盘原始 HTML；也可显式强制落盘
    python tests/live_ascii2d_check.py --dump-html

    # 离线自检请求构造（不发网络请求，专门核对 multipart 字段名与路径）
    python tests/live_ascii2d_check.py --self-test

说明：
- ascii2d 站点在日本，**大陆访问通常需要代理**（AstrBot 有全局 ``http_proxy``）；
  本脚本会用系统 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量。
- ascii2d **不需要账号 / API Key / Cookie**，本脚本不会打印任何敏感信息。
- 解析结果为 0 条时，脚本会**自动**把原始 HTML 落盘到项目内 ``recon/ascii2d_last.html`` 供排障。
- 连不通时脚本**优雅退出、退出码 0**，并给出可读排查建议。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path, PurePath

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用 test_core 里的 astrbot 桩（导入即完成 sys.modules 装配），使脚本可在无 AstrBot 环境运行
import tests.test_core as _stub  # noqa: E402,F401

import aiohttp  # noqa: E402

from astrbot_plugin_soutu_search.core.ascii2d_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    Ascii2dClient,
)

DEFAULT_IMAGE = PLUGIN_ROOT.parent / "recon" / "test.jpg"
# 默认 HTML 落盘路径：**必须位于项目内**（项目根/recon/ascii2d_last.html）
DEFAULT_DUMP_PATH = PLUGIN_ROOT.parent / "recon" / "ascii2d_last.html"


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def coerce_dump_path(value) -> Path | None:
    """把落盘路径参数归一化为 ``Path``；**只接受 ``str`` / ``pathlib.PurePath``**。

    刻意**不接受任意 ``os.PathLike``**：``unittest.mock.MagicMock`` 也满足
    ``isinstance(x, os.PathLike)`` 且 ``os.fspath(MagicMock())`` 不抛异常，若不加区分
    会让测试桩把垃圾目录写进仓库（本项目修过的坑）。无效类型 / 空串返回 ``None``。
    """
    if isinstance(value, PurePath):
        text = str(value).strip()
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text:
        return None
    return Path(text)


def extract_form_fields(form):
    """稳健抽取 ``aiohttp.FormData`` 的 ``(字段名, 值)`` 列表，返回 ``(fields, ok)``。

    兼容多种 ``_fields`` 元素形态（dict / 三元组 / 对象）；``ok=False`` 表示**无法内省**，
    与「字段名缺失」是两回事（调用方必须区分）。
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
    except Exception:  # noqa: BLE001
        return [], False


def format_field_value(value) -> str:
    """把表单字段值渲染为可读字符串（二进制只显示字节数，**绝不**打印内容）。"""
    if isinstance(value, (bytes, bytearray)):
        return f"<binary {len(value)}B>"
    return repr(value)


# --------------------------------------------------------------------------- #
# 请求拦截：捕获实际发出的字段名 / 请求头 / 原始 HTML / 最终响应 URL
# --------------------------------------------------------------------------- #
class _RespProxy:
    """包装 aiohttp 响应上下文管理器，顺带把原始 body 与最终 URL 记进 ``captured``。"""

    def __init__(self, resp, captured: dict, key: str):
        self._resp = resp
        self._captured = captured
        self._key = key
        self.status = None
        self.url = ""

    async def __aenter__(self):
        await self._resp.__aenter__()
        self.status = getattr(self._resp, "status", None)
        self.url = getattr(self._resp, "url", "")
        self._captured[self._key + "_status"] = self.status
        self._captured[self._key + "_url"] = str(self.url)
        return self

    async def __aexit__(self, *args):
        return await self._resp.__aexit__(*args)

    async def text(self):
        body = await self._resp.text()
        self._captured[self._key + "_body"] = body
        return body


def spy_client(client: Ascii2dClient) -> dict:
    """挂一个请求拦截器，记录实际发出的字段名 / 请求头 / 原始 body / 最终 URL。"""
    captured: dict = {}
    real_get_session = client._get_session

    async def patched_get_session():
        session = await real_get_session()
        if getattr(session, "_probe_patched", False):
            return session
        real_post = session.post
        real_get = session.get

        def spy_post(url, **kwargs):
            captured["post_url"] = url
            captured["headers"] = dict(kwargs.get("headers") or {})
            fields, ok = extract_form_fields(kwargs.get("data"))
            captured["fields_ok"] = ok
            captured["field_names"] = [name for name, _ in fields]
            captured["fields"] = [(name, format_field_value(value)) for name, value in fields]
            return _RespProxy(real_post(url, **kwargs), captured, "post")

        def spy_get(url, **kwargs):
            captured["get_url"] = url
            return _RespProxy(real_get(url, **kwargs), captured, "get")

        session.post = spy_post  # type: ignore[assignment]
        session.get = spy_get  # type: ignore[assignment]
        session._probe_patched = True
        return session

    client._get_session = patched_get_session  # type: ignore[assignment]
    return captured


def _print_request_selfcheck(captured: dict, base_url: str) -> None:
    """打印 ``[3/3]`` 请求自检块（区分「无法内省」与「字段名错误」）。"""
    _hr("[3/3] 请求自检（确认 multipart 字段名 / 路径 / 请求头）")
    print("POST URL  :", captured.get("post_url") or f"{base_url}/search/file")
    print("表单字段  :", captured.get("fields"))
    headers = captured.get("headers", {})
    print("Referer   :", headers.get("Referer"))
    print("Origin    :", headers.get("Origin"))
    print("UA        :", (headers.get("User-Agent") or "")[:60])
    if captured.get("get_url"):
        print("bovw GET  :", captured.get("get_url"))
    if captured.get("fields_ok"):
        names = captured.get("field_names", [])
        if "file" in names:
            print("含 file 字段 ? True（正确）")
        else:
            print(f"含 file 字段 ? False（!!! 异常：字段名为 {names!r}，请把本段输出贴给开发者）")
    else:
        print("含 file 字段 ? 无法内省 FormData（**不是**字段名错误；可能是 aiohttp 版本差异）")


def _write_dump(captured: dict, dump_path: Path) -> None:
    """把原始 HTML 落盘到项目内路径（追加 bovw 二次响应，便于对照）。"""
    body = captured.get("get_body") or captured.get("post_body") or ""
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(body, encoding="utf-8", errors="replace")
        print(f"\n[排障] 已把原始 HTML 落盘到: {dump_path}（{len(body)} 字符）")
        if captured.get("get_body") and captured.get("post_body"):
            print("       （本次含 bovw 二次请求，落盘的是 **bovw** 响应；color 响应未落盘）")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[排障] 落盘失败: {exc}")


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
        print("结论: 连接超时 ❌ —— 大陆访问通常需要代理（设置 HTTPS_PROXY 或使用 --base-url 指定反代）")
    except aiohttp.ClientConnectorCertificateError as exc:
        print(f"结论: TLS 证书错误 ❌（{exc}）")
    except aiohttp.ClientConnectorError as exc:
        print(f"结论: 无法连接 ❌（{exc}）—— 大陆访问通常需要代理")
    except aiohttp.ClientError as exc:
        print(f"结论: 网络错误 ❌（{type(exc).__name__}: {exc}）")
    return False


async def do_search(*, base_url: str, image_path: Path, bovw: bool, timeout: int, dump_path: Path, force_dump: bool) -> None:
    _hr(f"[2/3] 真实反查: POST {base_url}/search/file  (bovw={bovw})")

    if not image_path.exists():
        print(f"!! 找不到测试图片: {image_path}（可用 --image 指定）")
        return
    image = image_path.read_bytes()
    print(f"输入图: {image_path} ({len(image)} bytes)")
    print("说明: ascii2d 无需 API Key / Cookie。")

    client = Ascii2dClient(base_url=base_url, bovw=bovw, timeout=timeout)
    captured = spy_client(client)

    outcome = None
    error: Exception | None = None
    try:
        outcome = await client.search(image, filename=image_path.name, mime="image/jpeg")
    except Exception as exc:  # noqa: BLE001
        error = exc
    finally:
        await client.close()

    # 无论成功与否都打印请求自检——请求已构造/发出，这正是核对 multipart 字段名的关键
    _print_request_selfcheck(captured, base_url)

    if error is not None:
        print(f"\n!! 反查失败: {type(error).__name__}: {error}")
        print("   排查建议：① 确认已配置代理（HTTPS_PROXY）；② 用 --base-url 指定镜像/反代；")
        print("             ③ 若为 403/空响应，可能被站点拦截或需更换出口；")
        print("             ④ 用 --dump-html 落盘原始响应进一步分析。")
        if force_dump:
            _write_dump(captured, dump_path)
        return

    _hr("解析结果")
    print(f"结果条数: {len(outcome.results)}")
    print("warnings :", outcome.warnings)
    print("请求 URL       :", captured.get("post_url") or f"{base_url}/search/file")
    print("响应最终 URL   :", captured.get("post_url_url") or "（未捕获到，可能 aiohttp 版本差异）")
    if captured.get("get_url"):
        print("bovw 请求 URL  :", captured.get("get_url"))
        print("bovw 响应 URL  :", captured.get("get_url_url") or "（未捕获到）")

    if not outcome.results:
        print("（解析 0 条：可能确实无匹配，或站点改版导致选择器失效，或返回了挑战页/空页）")
        print("  → 已自动落盘原始 HTML 以便排障（见下）。")
        _write_dump(captured, dump_path)
        return

    if force_dump:
        _write_dump(captured, dump_path)

    for i, result in enumerate(outcome.results[:3], start=1):
        print(f"\n{i}. [{result.source}] {result.title or '（无标题）'}")
        print(f"   画师  : {result.extra.get('author')}  {result.extra.get('author_url') or ''}")
        print(f"   链接  : {result.url or '（无链接）'}")
        print(f"   来源标记: {result.extra.get('source_mark')} | detail: {result.extra.get('detail')}")
        print(f"   缩略图: {'有（仅 nsfw_send_image=True 时使用）' if result.thumbnail else '无'}")


async def do_self_test(*, base_url: str, bovw: bool) -> None:
    """离线自检：**不发任何网络请求**，仅验证请求构造（路径 / 字段名 / 请求头）。"""
    client = Ascii2dClient(base_url=base_url, bovw=bovw)
    form = client.build_form(b"\xff\xd8\xffSELFTEST", filename="selftest.jpg", mime="image/jpeg")
    fields, ok = extract_form_fields(form)
    captured = {
        "post_url": f"{base_url}/search/file",
        "headers": client.build_headers(),
        "fields_ok": ok,
        "field_names": [name for name, _ in fields],
        "fields": [(name, format_field_value(value)) for name, value in fields],
    }
    _print_request_selfcheck(captured, base_url)
    print("\n说明：本自检**不联网**，仅验证请求构造（multipart 字段名 / 路径 / 请求头）。")
    print("      真实联网的解析结果，请在不带 --self-test 的情况下运行本脚本。")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ascii2d 联网校验脚本")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE), help="待反查的图片路径")
    parser.add_argument("--base-url", default=os.environ.get("ASCII2D_BASE_URL", DEFAULT_BASE_URL),
                        help="接口地址（可填镜像/反代；默认读 ASCII2D_BASE_URL 或官方站点）")
    parser.add_argument("--bovw", action="store_true", help="使用特征检索（bovw，多消耗一次请求）")
    parser.add_argument("--timeout", type=int, default=60, help="请求超时（秒）")
    parser.add_argument("--dump-html", action="store_true",
                        help="把原始 HTML 落盘到项目内（默认 recon/ascii2d_last.html）")
    parser.add_argument("--dump-path", default=str(DEFAULT_DUMP_PATH),
                        help="HTML 落盘路径（**需为项目内路径**；仅接受字符串）")
    parser.add_argument("--skip-connectivity", action="store_true", help="跳过连通性检查")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检请求构造（不发网络请求）：验证 multipart 字段名与路径")
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()
    base_url = str(args.base_url).rstrip("/")

    dump_path = coerce_dump_path(args.dump_path) or DEFAULT_DUMP_PATH

    if args.self_test:
        print("ascii2d 请求构造离线自检（--self-test）")
        print(f"base_url = {base_url}")
        await do_self_test(base_url=base_url, bovw=args.bovw)
        print("\n完成。")
        return

    print("ascii2d 联网校验")
    print(f"base_url = {base_url}")
    print(f"image    = {args.image}")
    print("提示：ascii2d 站点在日本，大陆访问通常需要代理。")

    if not args.skip_connectivity:
        reachable = await check_reachability(base_url, timeout=min(args.timeout, 15))
        if not reachable:
            print("\n⚠️ 连通性检查未通过。仍会尝试直接反查（有时首页被墙但接口可用）；")
            print("   若失败，请设置代理（HTTPS_PROXY）后重试，或用 --base-url 指定镜像/反代。")

    await do_search(
        base_url=base_url,
        image_path=Path(args.image),
        bovw=args.bovw,
        timeout=args.timeout,
        dump_path=dump_path,
        force_dump=args.dump_html,
    )
    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
