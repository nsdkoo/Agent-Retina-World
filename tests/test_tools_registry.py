"""ToolRegistry 基座测试（纯逻辑，全平台可跑）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.tools.registry import (
    PlatformError,
    RiskLevel,
    ToolRegistry,
    ToolSpec,
)
from screen_agent.voice.executor import ActionResult


def _ok(**_params) -> ActionResult:
    return ActionResult(success=True, message="ok")


def _boom(**_params) -> ActionResult:
    raise RuntimeError("炸了")


class RegistryTests(unittest.TestCase):
    def test_register_get_run(self) -> None:
        reg = ToolRegistry()
        reg.register(ToolSpec("demo.run", "演示", _ok))
        result = reg.run("demo.run")
        self.assertTrue(result.success)

    def test_unknown_tool(self) -> None:
        reg = ToolRegistry()
        result = reg.run("nope")
        self.assertFalse(result.success)
        self.assertIn("未知工具", result.message)

    def test_exception_becomes_failure(self) -> None:
        reg = ToolRegistry()
        reg.register(ToolSpec("bad.run", "会炸", _boom))
        result = reg.run("bad.run")
        self.assertFalse(result.success)
        self.assertIn("炸了", result.message)

    def test_high_risk_confirm_gate(self) -> None:
        reg = ToolRegistry(confirm_fn=lambda spec, _p: False)  # 一律拒绝
        reg.register(ToolSpec("danger.run", "危险", _ok, risk=RiskLevel.HIGH))
        result = reg.run("danger.run")
        self.assertFalse(result.success)
        self.assertIn("已取消", result.message)

        reg2 = ToolRegistry(confirm_fn=lambda spec, _p: True)
        reg2.register(ToolSpec("danger.run", "危险", _ok, risk=RiskLevel.HIGH))
        self.assertTrue(reg2.run("danger.run").success)

    def test_low_risk_skips_confirm(self) -> None:
        called = {"n": 0}

        def confirm(_spec, _p) -> bool:
            called["n"] += 1
            return True

        reg = ToolRegistry(confirm_fn=confirm)
        reg.register(ToolSpec("safe.run", "安全", _ok, risk=RiskLevel.LOW))
        reg.run("safe.run")
        self.assertEqual(called["n"], 0)

    def test_list_tools_filter(self) -> None:
        reg = ToolRegistry()
        reg.register(ToolSpec("a.low", "低", _ok, risk=RiskLevel.LOW))
        reg.register(ToolSpec("b.high", "高", _ok, risk=RiskLevel.HIGH))
        self.assertEqual(len(reg.list_tools()), 2)
        self.assertEqual([s.name for s in reg.list_tools(max_risk=RiskLevel.LOW)], ["a.low"])

    def test_platform_error_message(self) -> None:
        def _platform_only(**_p) -> ActionResult:
            raise PlatformError("仅 Windows")

        reg = ToolRegistry()
        reg.register(ToolSpec("win.only", "仅 Win", _platform_only))
        result = reg.run("win.only")
        self.assertFalse(result.success)
        self.assertIn("仅支持 Windows", result.message)


if __name__ == "__main__":
    unittest.main()
