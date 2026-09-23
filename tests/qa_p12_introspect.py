"""QA 独立复验 P1-2：extract_form_fields 的内省区分 + 老版本 aiohttp 兼容。

不依赖工程师的 test_saucenao_live_script 结论，独立构造多种 _fields 形态。
运行:
    python tests/qa_p12_introspect.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.live_saucenao_check as live  # noqa: E402  (装配 astrbot 桩)


def show(label, form_entries):
    class F:
        pass

    f = F()
    f._fields = form_entries
    fields, ok = live.extract_form_fields(f)
    print(f"{label:<46} ok={ok!s:<5} names={[n for n, _ in fields]}")


print("=" * 78)
print("A. 真实 aiohttp.FormData")
print("=" * 78)
import aiohttp  # noqa: E402

form = aiohttp.FormData()
form.add_field("file", b"\xff\xd8\xffIMG", filename="q.jpg", content_type="image/jpeg")
print("  _fields 原始形态:", type(form._fields).__name__,
      "-> 元素类型:", type(form._fields[0]).__name__)
fields, ok = live.extract_form_fields(form)
print(f"  -> ok={ok} names={[n for n, _ in fields]}")

print("\n" + "=" * 78)
print("B. 老版本 aiohttp 兼容形态（逐个构造）")
print("=" * 78)
# aiohttp 3.8/3.9 早期：list[dict]
show("B1 dict 形态(含 name/value)", [{"name": "file", "value": b"x"}])
# aiohttp 3.14：list[tuple(info, headers, value)]，info 为 dict
show("B2 tuple(info=dict) 形态", [({"name": "file"}, {}, b"x")])
# 更老：info 为 (name, filename) 元组
show("B3 tuple(info=tuple) 形态", [(("file", "q.jpg"), {}, b"x")])
# object-with-get
class Entry:
    def get(self, key):
        return {"name": "file", "value": b"z"}.get(key)


show("B4 object.get 形态", [Entry()])

print("\n" + "=" * 78)
print("C. 「无法内省」vs「字段名缺失」的区分（本轮修复核心）")
print("=" * 78)
show("C1 未知元素形态(int) -> 期望 ok=False", [1, 2, 3])
show("C2 无 _fields 属性 -> 期望 ok=False", None)
show("C3 name 取不到 -> 期望 ok=False", [{"value": b"x"}])
show("C4 能内省但无 file -> 期望 ok=True 且不含 file", [{"name": "other", "value": b"x"}])

# 打印函数层面：C1/C4 各自的 [3/3] 文案
print("\n--- C1（无法内省）走 _print_request_selfcheck 的实际文案 ---")


class Weird:
    _fields = [1, 2, 3]


wfields, wok = live.extract_form_fields(Weird())
live._print_request_selfcheck({
    "url": "u", "params": {"api_key": "SUPER_SECRET_KEY_12345"}, "headers": {},
    "fields_ok": wok, "field_names": [n for n, _ in wfields],
    "fields": [(n, live.format_field_value(v)) for n, v in wfields],
})

print("\n--- C4（能内省但字段名不是 file）走 _print_request_selfcheck 的实际文案 ---")


class Missing:
    _fields = [{"name": "other", "value": b"x"}]


mfields, mok = live.extract_form_fields(Missing())
live._print_request_selfcheck({
    "url": "u", "params": {}, "headers": {},
    "fields_ok": mok, "field_names": [n for n, _ in mfields],
    "fields": [(n, live.format_field_value(v)) for n, v in mfields],
})
