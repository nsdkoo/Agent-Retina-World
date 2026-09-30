"""通道级隐私策略的测试。

对标 Screenpipe 的 per-pipe 权限（allow-apps / deny-apps / time-range 三层执行）。

改成通道级要守住两件事：

1. **默认行为与旧版完全等价** —— 旧的 `verdict` 是"一眼定生死"的二值结论，
   新版默认配置下必须给出一样的答案，否则升级即事故
2. **但能看出是哪一层被拦的** —— 这是细粒度真正的价值：旧版只能告诉你"不能记"，
   你没法知道"是标题不能留"还是"正文不能留"
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.capture.privacy import (
    CHANNELS,
    ChannelPolicy,
    PrivacyGate,
)
from screen_agent.eval.dataset import GoldenSet, PrivacyCase
from screen_agent.eval.runner import Evaluator


class ChannelVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = PrivacyGate()

    def test_private_window_blocks_skeleton_and_meta(self) -> None:
        """私密窗口：连骨架和标题都不留。"""
        decision = self.gate.evaluate("1Password - 保险库", "1password.exe", [])
        self.assertFalse(decision.allowed)
        self.assertIn("event", decision.blocked_channels())
        self.assertIn("window_meta", decision.blocked_channels())
        self.assertEqual(decision.reason, "私密窗口")

    def test_sensitive_content_blocks_only_text(self) -> None:
        """含密钥：只有文本通道该被拦，标题和骨架可以留——这就是细粒度的价值。"""
        decision = self.gate.evaluate(
            "main.py - VS Code", "编辑器", ["sk-abcdef1234567890abcdef1234567890"]
        )
        self.assertFalse(decision.allowed)
        self.assertIn("a11y_text", decision.blocked_channels())
        self.assertNotIn("window_meta", decision.blocked_channels())
        self.assertNotIn("event", decision.blocked_channels())

    def test_ocr_text_judged_separately(self) -> None:
        """OCR 文本单独成通道：敏感判定各算各的，便于排查是哪一路出的问题。"""
        clean = self.gate.evaluate("记事本", "notepad.exe", ["普通内容"])
        dirty = self.gate.evaluate(
            "记事本", "notepad.exe", ["普通内容"], ocr_text="6222 0212 3456 7890"
        )
        self.assertTrue(clean.allowed)
        self.assertFalse(dirty.allowed)
        self.assertIn("ocr_text", dirty.blocked_channels())

    def test_active_hours_blocks_skeleton(self) -> None:
        gate = PrivacyGate(active_hours=(9, 17))
        inside = gate.evaluate("main.py", "编辑器", ["内容"], moment=datetime(2026, 9, 30, 10, 0))
        outside = gate.evaluate("main.py", "编辑器", ["内容"], moment=datetime(2026, 9, 30, 22, 0))
        self.assertTrue(inside.allowed)
        self.assertFalse(outside.allowed)
        self.assertEqual(outside.reason, "不在记录时段")

    def test_screenshot_does_not_participate_by_default(self) -> None:
        """截图默认不采集：它不该把 allowed 拖成 False，否则兼容性直接崩。"""
        decision = self.gate.evaluate("main.py", "编辑器", ["内容"])
        shot = decision.channel("screenshot")
        self.assertIsNotNone(shot)
        self.assertFalse(shot.allowed)
        self.assertFalse(shot.participates)
        self.assertTrue(decision.allowed, "未启用的截图通道影响了整体放行")

    def test_screenshot_enabled_participates(self) -> None:
        gate = PrivacyGate(screenshot_enabled=True)
        blocked = gate.evaluate("1Password", "1password.exe", [])
        self.assertIn("screenshot", blocked.blocked_channels())

    def test_clean_case_passes_every_channel(self) -> None:
        decision = self.gate.evaluate("main.py - VS Code", "编辑器", ["def f(): pass"])
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "")
        self.assertEqual(decision.blocked_channels(), [])


class VerdictCompatibilityTests(unittest.TestCase):
    """旧接口在新实现下必须给出同样的答案——升级不能变成事故。"""

    CASES = [
        ("1Password - 保险库", "1password.exe", []),
        ("中国建设银行 - 个人网银", "chrome.exe", []),
        ("main.py - Visual Studio Code", "编辑器", ["def f(): pass"]),
        ("记事本", "notepad.exe", ["sk-abcdef1234567890abcdef1234567890"]),
        ("订单详情", "chrome.exe", ["6222 0212 3456 7890"]),
        ("Q3 复盘 - WPS", "wps.exe", ["营收环比增长 12%"]),
    ]

    def test_verdict_matches_evaluate(self) -> None:
        gate = PrivacyGate()
        for title, process, texts in self.CASES:
            with self.subTest(title=title):
                allowed, reason = gate.verdict(title, process, texts)
                decision = gate.evaluate(title, process, texts)
                self.assertEqual(allowed, decision.allowed)
                self.assertEqual(reason, decision.reason)

    def test_verdict_blocks_the_same_three_layers(self) -> None:
        """旧版的三层拦截逻辑一个都不能少。"""
        gate = PrivacyGate()
        self.assertFalse(gate.verdict("1Password", "1password.exe", [])[0])
        self.assertFalse(gate.verdict("记事本", "notepad.exe", ["ghp_" + "a" * 30])[0])
        self.assertTrue(gate.verdict("main.py", "编辑器", ["普通内容"])[0])

    def test_all_channels_are_declared(self) -> None:
        decision = PrivacyGate().evaluate("main.py", "编辑器", ["内容"])
        for name in CHANNELS:
            self.assertIn(name, decision.channels, f"通道 {name} 没有给出裁决")


class ChannelExpectationEvalTests(unittest.TestCase):
    """通道级期望要能在评测里被验证。"""

    def test_channel_accuracy_computed(self) -> None:
        golden = GoldenSet(
            privacy=[
                PrivacyCase(
                    "c1", "main.py", "编辑器", expect_blocked=False,
                    texts=["sk-abcdef1234567890abcdef1234567890"],
                    channel_expectations={"a11y_text": False, "window_meta": True},
                ),
                PrivacyCase(
                    "c2", "1Password", "1password.exe", expect_blocked=True,
                    channel_expectations={"event": False, "window_meta": False},
                ),
            ]
        )
        report = Evaluator(privacy=PrivacyGate()).run(golden)
        self.assertEqual(report.channel_checked, 4)
        self.assertEqual(report.channel_accuracy, 1.0, "通道级期望没有全部命中")

    def test_channel_expectation_mismatch_is_counted(self) -> None:
        """故意写反的期望要能被抓出来，否则这个指标等于没算。"""
        golden = GoldenSet(
            privacy=[
                PrivacyCase(
                    "c1", "main.py", "编辑器", expect_blocked=False,
                    texts=["普通内容"],
                    channel_expectations={"a11y_text": False},   # 实际会放行
                ),
            ]
        )
        report = Evaluator(privacy=PrivacyGate()).run(golden)
        self.assertEqual(report.channel_checked, 1)
        self.assertEqual(report.channel_accuracy, 0.0)

    def test_samples_without_channel_expectations_are_ignored(self) -> None:
        golden = GoldenSet(privacy=[PrivacyCase("c1", "main.py", "编辑器", False, ["内容"])])
        report = Evaluator(privacy=PrivacyGate()).run(golden)
        self.assertEqual(report.channel_checked, 0)


class PolicyExtensionTests(unittest.TestCase):
    """策略对象留了扩展位，先确认它可构造、不影响默认路径。"""

    def test_policy_can_be_constructed(self) -> None:
        policy = ChannelPolicy(allow_apps=("code.exe",), deny_apps=("qq.exe",), redact_secrets=True)
        self.assertEqual(policy.allow_apps, ("code.exe",))
        self.assertTrue(policy.redact_secrets)

    def test_gate_with_policies_still_defaults_safe(self) -> None:
        gate = PrivacyGate(policies={"a11y_text": ChannelPolicy(redact_secrets=True)})
        self.assertFalse(gate.evaluate("1Password", "1password.exe", []).allowed)


if __name__ == "__main__":
    unittest.main()
