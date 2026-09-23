"""QA 联网端到端：驱动插件 ``book_cmd``（「搜本」）指令走完整链路（访问控制放行 → 取图 → 搜索 → 回复）。

使用 recon/test.jpg 的 data URI 作为图片来源，避免依赖 QQ 图床；真实访问 soutubot.moe。
（自 0.4.0 起 soutubot 以图搜图只由「搜本」触发，故本探针驱动 ``book_cmd``。）
运行::
    python tests/qa_live_plugin.py
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401

from astrbot_plugin_soutu_search.main import ACCESS_DENIED_TEXT, SoutuSearchPlugin  # noqa: E402

TEST_IMG = PLUGIN_ROOT.parent / "recon" / "test.jpg"
DATA_URI = "data:image/jpeg;base64," + base64.b64encode(TEST_IMG.read_bytes()).decode()


class Ev:
    def __init__(self, umo, text="/搜本"):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = type("M", (), {"group_id": None, "message_id": "m1"})()
        self.message = []

    def get_message_str(self):
        return self.message_str

    def plain_result(self, t):
        return ("plain", t)

    def chain_result(self, c):
        return ("chain", c)


async def main():
    # 访问控制：白名单放行 umo-A（仅指令通道）
    p = SoutuSearchPlugin(object(), {
        "access_mode": "whitelist",
        "whitelist": ["umo-A"],
        "cache_ttl": 0,
        "request_timeout": 60,
    })

    # 走真实取图：把 data URI 注入事件组件
    async def fake_from_event(event):
        return await p.image_source.from_source(DATA_URI)

    p.image_source.from_event = fake_from_event  # type: ignore

    out_allowed = [x async for x in p.book_cmd(Ev("umo-A"), "")]
    print(f"[放行会话] 输出条数={len(out_allowed)}")
    if out_allowed:
        blocks = out_allowed[0]
        print(f"  回复类型={blocks[0]} 组件数={len(blocks[1]) if blocks[0] == 'chain' else 'n/a'}")
        # 确认不含 image 组件（NSFW 默认关闭）
        comps = blocks[1] if blocks[0] == "chain" else []
        kinds = [type(c).__name__ for c in comps]
        print(f"  组件类型={kinds}")
        print(f"  含 Image 组件={'是' if any(k == '_Image' or k == 'Image' for k in kinds) else '否'}")
    else:
        print("  !!! 放行会话未产出任何回复（可能搜索失败或无命中）")

    # 受限会话（白名单外）→ 恰好一条拒绝提示
    out_denied = [x async for x in p.book_cmd(Ev("umo-B"), "")]
    denied_ok = len(out_denied) == 1 and out_denied[0][1] == ACCESS_DENIED_TEXT
    print(f"[受限会话] 输出条数={len(out_denied)}（期望 1，且为拒绝提示={denied_ok}）")

    await p.terminate()


asyncio.run(main())
