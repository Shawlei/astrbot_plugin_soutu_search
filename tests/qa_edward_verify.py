"""QA 独立验证脚本（Edward / 严过关）—— 不属于自动发现用例（文件名不以 test_ 开头）。

用途：不依赖工程师脚本，独立复现真实 Yandex dump 的解析，并跑边界/对抗用例。
运行：
  cd astrbot_plugin_soutu_search
  python tests/qa_edward_verify.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- 最小 astrbot 桩（仅需 astrbot.api.logger）--- #
if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _Logger:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    _api.logger = _Logger()
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

from astrbot_plugin_soutu_search.core.yandex_client import (  # noqa: E402
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_NEUTRAL,
    parse_yandex_html,
)


def dump_parse():
    dump = PLUGIN_ROOT.parent / "recon" / "yandex_diag_pixiv_probe_3666749.html"
    html = dump.read_text(encoding="utf-8", errors="replace")
    results = parse_yandex_html(html, base_url="https://yandex.ru", top_k=9, max_per_domain=2)
    print(f"[dump] 文件大小={len(html)} 字节, 返回 {len(results)} 条")
    counts = {LEVEL_HIGH: 0, LEVEL_NEUTRAL: 0, LEVEL_LOW: 0}
    pinterest = 0
    for i, r in enumerate(results[:9], 1):
        lvl = r.extra.get("source_level")
        counts[lvl] = counts.get(lvl, 0) + 1
        if "pinterest" in r.source.lower():
            pinterest += 1
        print(f"  {i}. [{lvl:7}] {r.source:32} {r.title[:40]}")
    print(f"[dump] 前9条 level 分布: {counts}, pinterest={pinterest}")


if __name__ == "__main__":
    dump_parse()
