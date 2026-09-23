"""本地图片来源白名单修复测试（线上 Bug：AstrBot 临时目录图片被拒）。

背景：AstrBot 收到图片后先落盘到 ``<data>/temp/media_image_*.jpg``，再把**本地路径**塞进
``Image.file``；而插件原先只放行**插件自身数据目录**，导致「发图搜图」永远被拒。

本文件覆盖：
- **AstrBot 临时目录图片被接受**：模拟 ``<data>/plugin_data/<plugin>`` + ``<data>/temp/...`` 结构；
- **越权路径仍被拒**（证明没有放宽过头）；
- ``extra_allowed_roots`` 逃生口：合法项可用、非法类型被忽略且不崩；
- 配置一致性 **20 ↔ 20**（自动搜图相关配置项已彻底移除）。

复用 tests/test_core.py 的 astrbot 桩（import 即完成 sys.modules 装配）。
运行::
    python -m unittest tests.test_local_image_roots -v
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search import main as main_mod  # noqa: E402
from astrbot_plugin_soutu_search.core.image_source import ImageSource  # noqa: E402
from astrbot_plugin_soutu_search.main import (  # noqa: E402
    SoutuSearchPlugin,
    _to_path_list,
)

# 合法 JPEG 魔数（FFD8FF），可通过对图片内容的魔数校验
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 48
CONF_SCHEMA = PLUGIN_ROOT / "_conf_schema.json"


def run(coro):
    return asyncio.run(coro)


def build_astrbot_layout(base: Path) -> tuple[Path, Path, Path]:
    """构造 ``<base>/astrbot/data`` 布局，返回 ``(data_root, plugin_dir, temp_dir)``。

    - ``plugin_dir = <data>/plugin_data/astrbot_plugin_soutu_search``
    - ``temp_dir   = <data>/temp``
    """
    data_root = base / "astrbot" / "data"
    plugin_dir = data_root / "plugin_data" / "astrbot_plugin_soutu_search"
    temp_dir = data_root / "temp"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    return data_root, plugin_dir, temp_dir


def make_plugin(plugin_dir: Path, **cfg) -> SoutuSearchPlugin:
    """构造插件，并把 ``StarTools.get_data_dir`` 指向 ``plugin_dir``（模拟真实 AstrBot 布局）。"""
    with patch.object(main_mod.StarTools, "get_data_dir", return_value=str(plugin_dir)):
        return SoutuSearchPlugin(object(), cfg)


# ===========================================================================
# 1. AstrBot 临时目录图片必须被接受（核心修复）
# ===========================================================================
class TestAstrbotTempAccepted(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="soutu_roots_"))
        self.data_root, self.plugin_dir, self.temp_dir = build_astrbot_layout(self._tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_within_allowed_accepts_astrbot_temp_image(self):
        """真实形态 ``<data>/temp/media_image_*.jpg`` 必须被判定为「在允许目录内」。"""
        media = self.temp_dir / "media_image_20260923162512335_f73c.jpg"
        media.write_bytes(JPEG)

        plugin = make_plugin(self.plugin_dir)
        self.assertTrue(
            plugin.image_source._within_allowed(media),
            "AstrBot 临时目录图片应被接受（修复前会被拒）",
        )

    def test_from_source_reads_astrbot_temp_image(self):
        """端到端：``from_source`` 能真正读取该临时图片（不再抛 PermissionError）。"""
        media = self.temp_dir / "media_image_20260923162345111_abcd.jpg"
        media.write_bytes(JPEG)

        plugin = make_plugin(self.plugin_dir)

        async def go():
            try:
                return await plugin.image_source.from_source(str(media))
            finally:
                await plugin.image_source.close()

        payload = run(go())
        self.assertEqual(payload.data, JPEG)
        self.assertEqual(payload.source_kind, "file")
        self.assertEqual(payload.filename, media.name)

    def test_allowed_roots_composition_is_minimal(self):
        """允许根目录应为 [插件数据目录, <data>/temp]；**不含**整个 data 目录。"""
        plugin = make_plugin(self.plugin_dir)
        roots = [str(r) for r in plugin.allowed_roots]

        self.assertIn(str(self.plugin_dir.resolve()), roots, "应放行插件自身数据目录")
        self.assertIn(str((self.data_root / "temp").resolve()), roots, "应放行 AstrBot data/temp")
        self.assertNotIn(str(self.data_root.resolve()), roots, "不得放行整个 AstrBot data 目录")
        self.assertNotIn(str((self.data_root / "plugin_data").resolve()), roots,
                         "不得放行整个 plugin_data 目录")

    def test_data_root_direct_file_rejected(self):
        """``<data>`` 直属文件（非 temp）仍必须被拒，证明只放行了 temp。"""
        probe = self.data_root / "astrbot_config.json"
        probe.write_text('{"secret": 1}', encoding="utf-8")

        plugin = make_plugin(self.plugin_dir)
        self.assertFalse(plugin.image_source._within_allowed(probe))
        with self.assertRaises(PermissionError):
            run(plugin.image_source.from_source(str(probe)))


# ===========================================================================
# 2. 越权路径仍被拒绝（证明没有放宽过头）
# ===========================================================================
class TestPrivilegePathsStillRejected(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="soutu_priv_"))
        self.data_root, self.plugin_dir, self.temp_dir = build_astrbot_layout(self._tmp)
        self.plugin = make_plugin(self.plugin_dir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_other_plugin_config_rejected(self):
        other_cfg = self.data_root / "plugin_data" / "other_plugin" / "config.json"
        self.assertFalse(self.plugin.image_source._within_allowed(other_cfg))

    def test_system_paths_rejected(self):
        for probe in (Path("/etc/passwd"), Path("C:/Windows/win.ini"), Path("C:/Windows/system32/")):
            self.assertFalse(
                self.plugin.image_source._within_allowed(probe),
                f"系统路径必须被拒: {probe}",
            )

    def test_other_plugin_real_file_rejected_end_to_end(self):
        """真实存在的越权文件（同盘 data 树内、但非本插件目录）也必须被拒。"""
        other_dir = self.data_root / "plugin_data" / "other_plugin"
        other_dir.mkdir(parents=True, exist_ok=True)
        secret = other_dir / "secret.png"
        secret.write_bytes(JPEG)

        with self.assertRaises(PermissionError):
            run(self.plugin.image_source.from_source(str(secret)))

    def test_reject_message_includes_allowed_roots(self):
        """拒绝日志需同时含被拒路径与当前允许根目录（可诊断性要求）。"""
        other_dir = self.data_root / "plugin_data" / "other_plugin"
        other_dir.mkdir(parents=True, exist_ok=True)
        secret = other_dir / "secret.png"
        secret.write_bytes(JPEG)

        try:
            run(self.plugin.image_source.from_source(str(secret)))
            self.fail("越权路径应被拒绝")
        except PermissionError as exc:
            message = str(exc)
            self.assertIn("不在允许目录内", message)
            self.assertIn("当前允许根目录", message)
            self.assertIn(str((self.data_root / "temp").resolve()), message)


# ===========================================================================
# 3. extra_allowed_roots 逃生口
# ===========================================================================
class TestExtraAllowedRoots(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="soutu_extra_"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_extra_root_accepted(self):
        """追加到 extra_allowed_roots 的目录内图片应被接受。"""
        _, plugin_dir, _ = build_astrbot_layout(self._tmp)
        extra_dir = self._tmp / "somewhere_else"
        extra_dir.mkdir(parents=True, exist_ok=True)
        img = extra_dir / "pic.jpg"
        img.write_bytes(JPEG)

        plugin = make_plugin(plugin_dir, extra_allowed_roots=[str(extra_dir)])
        self.assertIn(str(extra_dir.resolve()), [str(r) for r in plugin.allowed_roots])
        self.assertTrue(plugin.image_source._within_allowed(img))
        payload = run(plugin.image_source.from_source(str(img)))
        self.assertEqual(payload.data, JPEG)

    def test_valid_entries_kept_and_stripped(self):
        self.assertEqual(_to_path_list(["  /tmp/somewhere  "]), [Path("/tmp/somewhere")])
        # pathlib.PurePath 亦被接受
        self.assertEqual(_to_path_list([Path("/tmp/pp")]), [Path("/tmp/pp")])

    def test_invalid_entry_types_ignored_with_warning(self):
        """非 str/PurePath 的条目必须被忽略并告警，且不崩溃。"""
        with patch.object(main_mod.logger, "warning") as warn:
            result = _to_path_list([123, None, {"a": 1}, True, 4.5, b"bytes"])
        self.assertEqual(result, [])
        self.assertTrue(warn.called, "非法条目应触发 logger.warning")

    def test_bare_non_list_value_ignored(self):
        """顶层非 list 的值（含裸字符串 'abc' / 整数 123）应被忽略且不崩。"""
        for bad in ("abc", 123, 0.0, {"x": 1}, b"bytes"):
            with patch.object(main_mod.logger, "warning"):
                self.assertEqual(_to_path_list(bad), [], f"非法顶层值应被忽略: {bad!r}")

    def test_plugin_construction_with_invalid_config_no_crash(self):
        """把非法值塞进插件配置也不得崩溃。"""
        _, plugin_dir, _ = build_astrbot_layout(self._tmp)
        for bad in ("abc", 123, [None], [123, None, {}]):
            with patch.object(main_mod.logger, "warning"):
                plugin = make_plugin(plugin_dir, extra_allowed_roots=bad)
            self.assertEqual(plugin.extra_allowed_roots, [])

    def test_nested_sequence_flattened_and_filtered(self):
        got = _to_path_list([["/a", 123], [Path("/b"), None]])
        self.assertEqual(got, [Path("/a"), Path("/b")])


# ===========================================================================
# 4. 官方 API 探测 / 回溯回退
# ===========================================================================
class TestAstrbotDataRootResolution(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="soutu_api_"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_official_api_used_when_available(self):
        """若官方 API ``get_astrbot_data_path`` 可用，则采用其返回值。"""
        data_root = self._tmp / "official" / "data"
        data_root.mkdir(parents=True, exist_ok=True)
        fake_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
        fake_mod.get_astrbot_data_path = lambda: str(data_root)

        with patch.dict(sys.modules, {"astrbot.core.utils.astrbot_path": fake_mod}):
            plugin = SoutuSearchPlugin(object(), {})

        self.assertIn(str((data_root / "temp").resolve()), [str(r) for r in plugin.allowed_roots])

    def test_backtrack_fallback_when_api_missing(self):
        """无官方 API 时，从插件数据目录向上回溯找到 ``data`` 那一级。"""
        data_root, plugin_dir, _ = build_astrbot_layout(self._tmp)
        plugin = make_plugin(plugin_dir)
        self.assertIn(str((data_root / "temp").resolve()), [str(r) for r in plugin.allowed_roots])

    def test_no_data_ancestor_yields_no_temp(self):
        """数据目录树上没有名为 data 的祖先时，不强行放行 temp（优雅降级）。"""
        lonely = self._tmp / "lonely" / "plugin_dir"
        lonely.mkdir(parents=True, exist_ok=True)
        with patch.object(main_mod.StarTools, "get_data_dir", return_value=str(lonely)):
            plugin = SoutuSearchPlugin(object(), {})
        self.assertEqual([str(r) for r in plugin.allowed_roots], [str(lonely.resolve())])


# ===========================================================================
# 5. 配置一致性（20 ↔ 20）
# ===========================================================================
class TestConfigConsistency(unittest.TestCase):
    def _used_keys(self) -> set[str]:
        src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        used = set(re.findall(r'\bcfg\.get\(\s*"([^"]+)"', src))
        used |= set(re.findall(r'\bself\.config\.get\(\s*"([^"]+)"', src))
        return used

    def test_schema_and_code_keys_equal(self):
        schema = json.loads(CONF_SCHEMA.read_text(encoding="utf-8"))
        schema_keys = set(schema.keys())
        used = self._used_keys()
        self.assertEqual(len(schema_keys), 24, f"schema 键数应为 24，实际 {len(schema_keys)}")  # [工程师已改 #8] 20 -> 24（新增双源反查配置）
        self.assertEqual(schema_keys - used, set(), f"定义了但未使用: {schema_keys - used}")
        self.assertEqual(used - schema_keys, set(), f"使用了但未定义: {used - schema_keys}")

    def test_removed_auto_search_keys_absent(self):
        """自动搜图相关配置项应已彻底移除。"""
        schema = json.loads(CONF_SCHEMA.read_text(encoding="utf-8"))
        for gone in ("enable_auto_search", "auto_search_cooldown", "access_scope"):
            self.assertNotIn(gone, schema, f"{gone} 应已删除")

    def test_extra_allowed_roots_schema(self):
        schema = json.loads(CONF_SCHEMA.read_text(encoding="utf-8"))
        entry = schema["extra_allowed_roots"]
        self.assertEqual(entry["type"], "list")
        self.assertEqual(entry["default"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
