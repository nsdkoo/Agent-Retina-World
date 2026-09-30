"""隐私评测的有效性测试。

这组测试存在的唯一理由：**证明隐私召回不再是恒 1.0**。

背景：`runner.py` 原先写的是
    expected.append(not allowed)
    predicted.append(not allowed)
两个值同一个来源，于是 expected 恒等于 predicted、漏放数永远是 0、召回永远 1.0。
`PRIVACY_TOLERANCE = 0.0` 这条"漏放即事故"的门禁，从头到尾没拦过任何东西，
黄金集里 20 条隐私样本的 `expect_blocked` 标注也全程没被使用。

所以这里用"故意全放行"和"故意全拦截"两种假闸门来反向验证：
真在算，召回就该掉；恒真，怎么算都是 1.0。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.capture.privacy import PrivacyGate
from screen_agent.eval.dataset import GoldenSet
from screen_agent.eval.runner import (
    PRIVACY_ADVISORY_ONLY,
    EvalReport,
    Evaluator,
    compare_to_baseline,
)


class _AlwaysAllow:
    """什么都不拦。用它跑评测，漏放数应等于黄金集里所有 expect_blocked=True 的条数。"""

    def verdict(self, title, process_name, texts):  # noqa: ANN001
        return True, ""


class _AlwaysBlock:
    """什么都拦。误拦数应等于所有 expect_blocked=False 的条数。"""

    def verdict(self, title, process_name, texts):  # noqa: ANN001
        return False, "一律拦下"


class RecallIsRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.golden = GoldenSet.seed()
        self.should_block = sum(1 for c in self.golden.privacy if c.expect_blocked)
        self.should_pass = len(self.golden.privacy) - self.should_block

    def test_all_allow_gate_has_zero_recall(self) -> None:
        """全都放行 → 一条该拦的都没拦住 → 召回必须是 0，不是 1.0。"""
        report = Evaluator(privacy=_AlwaysAllow()).run(self.golden)
        self.assertEqual(report.privacy.fn, self.should_block)
        self.assertEqual(report.privacy.recall, 0.0)
        self.assertLess(report.privacy.recall, 1.0)

    def test_all_block_gate_has_perfect_recall_but_bad_precision(self) -> None:
        """全都拦 → 召回满分但精确率掉下来（说明两个方向都在算）。"""
        report = Evaluator(privacy=_AlwaysBlock()).run(self.golden)
        self.assertEqual(report.privacy.recall, 1.0)
        self.assertEqual(report.privacy.fp, self.should_pass)
        self.assertLess(report.privacy.precision, 1.0)

    def test_real_gate_uses_annotations(self) -> None:
        """真闸门：黄金集里标注该拦的都应被拦住。这条过了才说明标注与实现是对齐的。"""
        report = Evaluator(privacy=PrivacyGate()).run(self.golden)
        self.assertEqual(report.privacy.fn, 0, "有该拦没拦住的样本")
        self.assertEqual(report.privacy.recall, 1.0)

    def test_expected_comes_from_dataset_not_from_verdict(self) -> None:
        """期望必须取自黄金集标注。若改回拿判定结果当期望，这两组数会相等。"""
        report_allow = Evaluator(privacy=_AlwaysAllow()).run(self.golden)
        report_block = Evaluator(privacy=_AlwaysBlock()).run(self.golden)
        # 同一份数据、两种相反的闸门，召回必须不同；恒真的实现下两者都是 1.0
        self.assertNotEqual(
            report_allow.privacy.recall, report_block.privacy.recall,
            "两种相反闸门召回相同，说明期望不是来自数据集标注",
        )


class AdvisoryGateTests(unittest.TestCase):
    """「只报不拦」阶段的行为：隐私退化只给警告，不阻断。"""

    def _report(self, recall: float) -> EvalReport:
        report = EvalReport()
        report.activity.macro_f1 = 0.7
        report.privacy.tp = int(recall * 100)
        report.privacy.fn = 100 - report.privacy.tp
        return report

    def test_privacy_drop_blocks_when_not_advisory(self) -> None:
        baseline = {"metrics": {"activity_macro_f1": 0.6, "privacy_recall": 0.95}}
        current = self._report(0.5)
        passed, reasons = compare_to_baseline(
            current, baseline, privacy_tolerance=0.0
        )
        if PRIVACY_ADVISORY_ONLY:
            self.assertTrue(passed, "当前是只报不拦阶段，不该阻断")
            self.assertTrue(any("隐私召回" in r for r in reasons))
        else:
            self.assertFalse(passed)

    def test_activity_regression_still_blocks(self) -> None:
        """活动指标退化照旧阻断——只放宽隐私那一条，不是整个门禁失效。"""
        baseline = {"metrics": {"activity_macro_f1": 0.9, "privacy_recall": 0.0}}
        passed, reasons = compare_to_baseline(self._report(1.0), baseline)
        self.assertFalse(passed)
        self.assertTrue(any("macro-F1" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
