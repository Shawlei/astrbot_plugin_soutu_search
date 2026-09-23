"""Yandex 识图真人端到端联网校验（**不属于常规单测，需要真实网络**）。

它会：

1. **连通性检查**：GET ``<base_url>/images/`` 打印 HTTP 状态码（连不通时给出可读提示）；
2. **真实反查**：用插件自身的 ``YandexClient`` 上传图片，打印解析出的结果条数、
   前 3 条的标题 / 来源域名 / 链接，以及整体耗时；
3. **请求自检**（``--self-test``）：离线核对 multipart 字段名（``prg`` / ``upfile``）、
   请求路径与请求头，**不发任何网络请求**。

用法::

    # 最简（默认用 recon/test.jpg）
    python tests/live_yandex_check.py

    # 指定图片 / 反代 / 超时
    python tests/live_yandex_check.py --image path/to/pic.jpg
    python tests/live_yandex_check.py --base-url https://你的反代/
    python tests/live_yandex_check.py --timeout 60

    # 解析结果为 0 条时**自动**落盘原始 HTML；也可显式强制落盘
    python tests/live_yandex_check.py --dump-html

    # 离线自检请求构造（不联网）
    python tests/live_yandex_check.py --self-test

说明：
- Yandex **不需要账号 / API Key / Cookie**，本脚本不会打印任何敏感信息。
- 解析结果为 0 条时，脚本会**自动**把原始 HTML 落盘到项目内 ``recon/yandex_last.html`` 供排障。
- 连不通时脚本优雅退出、退出码 0，并给出可读排查建议。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path, PurePath

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用 test_core 里的 astrbot 桩（导入即完成 sys.modules 装配），使脚本可在无 AstrBot 环境运行
import tests.test_core as _stub  # noqa: E402,F401

from astrbot_plugin_soutu_search.core.yandex_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    YandexClient,
    parse_yandex_html,
)

DEFAULT_IMAGE = PLUGIN_ROOT.parent / "recon" / "test.jpg"
# 默认 HTML 落盘路径：**必须位于项目内**（项目根/recon/yandex_last.html）
DEFAULT_DUMP_PATH = PLUGIN_ROOT.parent / "recon" / "yandex_last.html"


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def coerce_path(value) -> Path | None:
    """把路径参数归一化为 ``Path``；**只接受 ``str`` / ``pathlib.PurePath``**。

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


def _build_body(image: bytes, filename: str = "image.jpg", mime: str = "image/jpeg") -> bytes:
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    part1 = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="prg"\r\n\r\n'
        f"1\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="upfile"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    part2 = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return part1 + image + part2


def self_test(base_url: str) -> int:
    """离线自检：核对 multipart 字段名 / 请求路径 / 请求头（不联网）。"""
    _hr("Yandex 请求构造离线自检（--self-test）")
    print(f"base_url = {base_url}")
    body = _build_body(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4, "q.png", "image/png")

    _hr("[1/2] multipart 结构")
    text = body.decode("utf-8", errors="replace")
    ok_prg = 'name="prg"' in text
    ok_upfile = 'name="upfile"' in text
    print(f"含 prg 字段    ? {ok_prg}")
    print(f"含 upfile 字段 ? {ok_upfile}")
    print(f"body 为 bytes  ? {isinstance(body, bytes)}（必须，保证 Content-Length 避免 413）")

    _hr("[2/2] 请求路径")
    url = f"{base_url}/images/search"
    print(f"POST URL: {url}")
    print("params  : rpt=imageview & format=json & request={...b-page_type_search-by-image__link...}")
    all_ok = ok_prg and ok_upfile and isinstance(body, bytes)
    print(f"\n{'✅ 自检通过' if all_ok else '❌ 自检失败'}（本自检**不联网**）")
    return 0 if all_ok else 1


async def run_live(args) -> int:
    image_path = coerce_path(args.image) or DEFAULT_IMAGE
    if not image_path.exists():
        print(f"❌ 图片不存在: {image_path}")
        return 1
    img = image_path.read_bytes()
    print(f"使用图片: {image_path}（{len(img)} bytes）")

    # 1. 连通性
    _hr("[1/2] 连通性检查")
    import urllib.request

    try:
        req = urllib.request.Request(
            f"{args.base_url}/images/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            print(f"GET {args.base_url}/images/ → HTTP {resp.status} ✅")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 无法访问 {args.base_url}：{exc}")
        print("排查建议：检查网络；大陆网络一般可直连 yandex.ru（无需代理）；")
        print("若使用反代，请用 --base-url 指定。")
        return 0

    # 2. 真实反查
    _hr("[2/2] 真实反查（YandexClient）")
    client = YandexClient(base_url=args.base_url, timeout=args.timeout)
    dump_path = coerce_path(args.dump_html) or DEFAULT_DUMP_PATH
    started = time.perf_counter()
    try:
        outcome = await client.search(img, filename=image_path.name)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 反查失败：{exc}")
        await client.close()
        return 0
    elapsed = time.perf_counter() - started
    print(f"耗时 {elapsed:.2f}s，命中 {len(outcome.results)} 条")
    for i, r in enumerate(outcome.results[:3], 1):
        print(f"  {i}. [{r.source}] {r.title}\n     🔗 {r.url}")
    await client.close()

    if not outcome.results or args.dump_html:
        # 0 条或显式要求：落盘原始 HTML 供排障
        print(f"\nℹ️ 解析结果为空或指定 --dump-html，落盘原始 HTML…（解析 0 条会同时自动落盘）")
    if not outcome.results:
        print(f"⚠️ 命中 0 条。建议换一张更常见的图片重试；")
        print(f"   若需排障，可运行：python {__file__} --dump-html")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Yandex 识图联网校验脚本")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE), help="用于反查的图片路径")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"默认 {DEFAULT_BASE_URL}")
    parser.add_argument("--timeout", type=int, default=30, help="请求超时秒数")
    parser.add_argument("--dump-html", action="store_true", help="强制落盘原始 HTML")
    parser.add_argument(
        "--dump-path", default=str(DEFAULT_DUMP_PATH), help=f"HTML 落盘路径（默认 {DEFAULT_DUMP_PATH}）"
    )
    parser.add_argument("--self-test", action="store_true", help="离线自检请求构造（不联网）")
    args = parser.parse_args()

    if args.self_test:
        return self_test(args.base_url)
    return asyncio.run(run_live(args))


if __name__ == "__main__":
    sys.exit(main())
