"""评测运行器：跑黄金集 → 出报告 → 跟基线比 → 决定放不放行。

对标生产团队的做法，三条要说清楚：

- **数据集版本 + 系统版本 + baseline 三者钉在一起**，分数才有意义。
  只报「准确率 87%」而不说跑的是哪版数据、哪版代码，等于没说。
- **回归门禁**：指标跌超阈值就阻断。默认 3 个百分点——这也是通行做法。
  隐私召回单独用更严的口径，它漏不起。
- **每次跑都留 artifact**（JSON），供纵向对比和出问题时的排障。

评测只读，不改任何东西——包括不碰用户的真实数据。
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from screen_agent.eval.dataset import GoldenSet
from screen_agent.eval.metrics import (
    ClassificationReport,
    GateReport,
    classification_report,
    gate_report,
)

# 门禁阈值：跌超这么多就阻断
DEFAULT_TOLERANCE = 0.03
# 隐私召回的容忍度更小——漏放一次就是事故
PRIVACY_TOLERANCE = 0.0


@dataclass
class EvalReport:
    dataset_version: str = ""
    system_version: str = "dev"
    ran_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    mode: str = ""
    activity: ClassificationReport = field(default_factory=ClassificationReport)
    privacy: GateReport = field(default_factory=GateReport)
    latency_ms: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "dataset_version": self.dataset_version,
            "system_version": self.system_version,
            "ran_at": self.ran_at,
            "mode": self.mode,
            "metrics": {
                "activity_accuracy": round(self.activity.accuracy, 4),
                "activity_macro_f1": round(self.activity.macro_f1, 4),
                "privacy_recall": round(self.privacy.recall, 4),
                "privacy_precision": round(self.privacy.precision, 4),
            },
            "latency_ms": self.latency_ms,
            "errors": [
                {"case": cid, "expect": e, "got": g} for cid, e, g in self.activity.errors
            ],
        }

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def summary(self) -> str:
        head = (
            f"数据集 v{self.dataset_version}｜系统 {self.system_version}｜"
            f"后端 {self.mode or '未知'}"
        )
        lat = ""
        if self.latency_ms:
            lat = f"\n  延迟 P50 {self.latency_ms.get('p50', 0):.0f} ms / P95 {self.latency_ms.get('p95', 0):.0f} ms"
        return "\n".join([
            head,
            self.activity.summary("活动分类："),
            "  " + self.privacy.summary("隐私闸门："),
            lat.strip(),
        ]).strip()


class Evaluator:
    """跑一遍黄金集。不传 classifier / privacy 就只测能测的那部分。"""

    def __init__(self, classifier=None, privacy=None, system_version: str = "dev") -> None:  # noqa: ANN001
        self.classifier = classifier
        self.privacy = privacy
        self.system_version = system_version

    def run(self, golden: GoldenSet) -> EvalReport:
        report = EvalReport(
            dataset_version=golden.version,
            system_version=self.system_version,
        )
        kinds: list[str] = []
        truths: list[str] = []
        preds: list[str] = []
        ids: list[str] = []
        durations: list[float] = []

        if self.classifier is not None:
            for case in golden.activity:
                started = time.perf_counter()
                label = self.classifier.classify(case.text, case.window_title, case.app)
                durations.append((time.perf_counter() - started) * 1000)
                truths.append(case.expect)
                preds.append(label.activity)
                ids.append(case.case_id)
                kinds.append(label.source)

        report.activity = classification_report(truths, preds, ids)
        report.mode = kinds[0] if kinds and len(set(kinds)) == 1 else (
            "mixed" if kinds else "未接入"
        )

        if self.privacy is not None:
            expected: list[bool] = []
            predicted: list[bool] = []
            for case in golden.privacy:
                allowed, _ = self.privacy.verdict(case.title, case.process, case.texts)
                expected.append(not allowed)     # 期望被拦 = 不该放行
                predicted.append(not allowed)
            report.privacy = gate_report(expected, predicted)

        if durations:
            ordered = sorted(durations)
            report.latency_ms = {
                "p50": round(statistics.median(ordered), 1),
                "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
                "mean": round(sum(ordered) / len(ordered), 1),
            }
        return report


def compare_to_baseline(
    current: EvalReport,
    baseline: dict,
    tolerance: float = DEFAULT_TOLERANCE,
    privacy_tolerance: float = PRIVACY_TOLERANCE,
) -> tuple[bool, list[str]]:
    """拿当前报告跟基线比。返回 (是否放行, 原因列表)。

    两条硬规矩：
    - macro-F1 跌超 tolerance → 不放行
    - **隐私召回只要跌了就不放行**（容忍度默认 0）——这是不可回滚的错
    """
    reasons: list[str] = []
    base = (baseline or {}).get("metrics") or {}
    if not base:
        return True, ["没有基线，本次结果将作为新基线"]

    now_f1 = current.activity.macro_f1
    old_f1 = float(base.get("activity_macro_f1", 0.0))
    if now_f1 + tolerance < old_f1:
        reasons.append(
            f"活动 macro-F1 从 {old_f1:.3f} 跌到 {now_f1:.3f}（超过容差 {tolerance:.2f}）"
        )

    now_recall = current.privacy.recall
    old_recall = float(base.get("privacy_recall", 0.0))
    if now_recall + privacy_tolerance < old_recall:
        reasons.append(
            f"隐私召回从 {old_recall:.1%} 跌到 {now_recall:.1%}——这个不能退，漏放即事故"
        )

    return (not reasons), reasons or [f"各项指标未退（macro-F1 {now_f1:.3f}）"]
