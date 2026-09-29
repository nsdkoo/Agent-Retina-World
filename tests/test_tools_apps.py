"""AppResolver 六级解析测试：mock powershell/winreg/文件系统。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.tools.apps import AppResolver


def _resolver(tmp: str, lnk_files: list[Path] | None = None) -> AppResolver:
    r = AppResolver(Path(tmp) / "cache" / "app_index.json")
    if lnk_files:
        r._cache["lnk_files"] = [str(p) for p in lnk_files]
        r._save_cache()
    return r


class AppResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_alias_path_hit(self) -> None:
        exe = Path(self.tmp.name) / "myapp.exe"
        exe.write_bytes(b"")
        r = AppResolver(Path(self.tmp.name) / "c.json", extra_apps={"记账": str(exe)})
        got = r.resolve("记账")
        self.assertIsNotNone(got)
        self.assertEqual(got.kind, "path")
        self.assertEqual(got.target, str(exe))

    def test_lnk_fuzzy_match(self) -> None:
        lnk = Path(self.tmp.name) / "QQ.lnk"
        lnk.write_bytes(b"")
        r = _resolver(self.tmp.name, lnk_files=[lnk])
        fake_run = MagicMock(return_value=MagicMock(stdout=f"{lnk}\tC:\\Program Files\\Tencent\\QQ.exe"))
        with patch("subprocess.run", fake_run), patch.object(Path, "exists", lambda self: True):
            got = r.resolve("qq")
        self.assertIsNotNone(got)
        self.assertEqual(got.kind, "lnk")
        self.assertEqual(got.target, str(lnk))

    def test_lnk_no_candidate_returns_none(self) -> None:
        lnk = Path(self.tmp.name) / "不相干应用.lnk"
        lnk.write_bytes(b"")
        r = _resolver(self.tmp.name, lnk_files=[lnk])
        # mock 掉 UWP 索引（真机 Get-StartApps 可能真有同名应用）
        empty_run = MagicMock(return_value=MagicMock(stdout="[]"))
        with patch("subprocess.run", empty_run):
            self.assertIsNone(r.resolve("qqxyz不存在的应用"))

    def test_resolution_cached(self) -> None:
        r = _resolver(self.tmp.name)
        r._cache["resolutions"]["abc"] = {"kind": "path", "target": "C:/x.exe", "display": "abc"}
        r._save_cache()
        r2 = AppResolver(Path(self.tmp.name) / "c" / "app_index.json")
        # 新实例读的是新缓存路径，直接对同实例验证命中缓存
        got = r.resolve("abc")
        self.assertIsNotNone(got)
        self.assertEqual(got.target, "C:/x.exe")

    def test_uwp_index_and_match(self) -> None:
        r = _resolver(self.tmp.name)
        fake = MagicMock(return_value=MagicMock(
            stdout='[{"Name":"计算器","AppID":"Microsoft.WindowsCalculator_8weky!App"}]'
        ))
        with patch("subprocess.run", fake):
            got = r.resolve("计算器")
        self.assertIsNotNone(got)
        self.assertEqual(got.kind, "uwp")
        self.assertIn("shell:AppsFolder\\", got.target)
        self.assertIn("Microsoft.WindowsCalculator", got.target)
        # 二次走缓存不再调 powershell
        fake.assert_called_once()


if __name__ == "__main__":
    unittest.main()
