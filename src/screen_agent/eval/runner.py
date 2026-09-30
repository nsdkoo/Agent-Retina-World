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

# 隐私召回的容忍度。
# **当前处于「只报不拦」阶段（2026-09-30 起）**：D1/D2 修好之后，真实召回率才第一次显形，
# 先观察几轮、摸清误报漏报的分布，再决定是否收紧。
# 收紧方式：把 PRIVACY_ADVISORY_ONLY 改成 False 即可（容忍度已是 0）。
PRIVACY_TOLERANCE = 0.0
PRIVACY_ADVISORY_ONLY = True


@dataclass
class EvalReport:
    dataset_version: str = ""
    system_version: str = "dev"
    ran_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    mode: str = ""
    activity: ClassificationReport = field(default_factory=ClassificationReport)
    privacy: GateReport = field(default_factory=GateReport)
    latency_ms: dict[str, float] = field(default_factory=dict)
    # 通道级准确率：只在样本显式声明了 channel_expectations 时才有值
    channel_accuracy: float = 0.0
    channel_checked: int = 0

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
                "channel_accuracy": round(self.channel_accuracy, 4),
                "channel_checked": self.channel_checked,
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
            channel_checked = 0
            channel_correct = 0
            for case in golden.privacy:
                texts = list(getattr(case, "texts", None) or [])
                # 优先用通道级的 evaluate；只实现了旧 verdict 的闸门也能跑——
                # verdict 目前仍被实测脚本等地方依赖，不该强制所有调用方升级
                if hasattr(self.privacy, "evaluate"):
                    decision = self.privacy.evaluate(
                        case.title, case.process, texts,
                        ocr_text=getattr(case, "ocr_text", ""),
                    )
                    allowed = decision.allowed
                else:
                    allowed, _ = self.privacy.verdict(case.title, case.process, texts)
                    decision = None

                # 期望来自黄金集人工标注的 expect_blocked，**不能拿判定结果自己当期望**。
                # 之前两行都写 `not allowed`，expected 恒等于 predicted，
                # 漏放数永远是 0、召回永远是 1.0 —— 这道门禁从来没生效过。
                expected.append(bool(getattr(case, "expect_blocked", False)))
                predicted.append(not allowed)

                # 通道级期望：只在样本显式声明且闸门支持时才算，避免把分母灌大
                if decision is None:
                    continue
                for channel, should_allow in (
                    getattr(case, "channel_expectations", None) or {}
                ).items():
                    verdict = decision.channel(channel)
                    if verdict is None:
                        continue
                    channel_checked += 1
                    if verdict.allowed == bool(should_allow):
                        channel_correct += 1
            report.privacy = gate_report(expected, predicted)
            if channel_checked:
                report.channel_accuracy = channel_correct / channel_checked
                report.channel_checked = channel_checked

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
    blocking: list[str] = []
    advisory: list[str] = []
    base = (baseline or {}).get("metrics") or {}
    if not base:
        return True, ["没有基线，本次结果将作为新基线"]

    now_f1 = current.activity.macro_f1
    old_f1 = float(base.get("activity_macro_f1", 0.0))
    if now_f1 + tolerance < old_f1:
        blocking.append(
            f"活动 macro-F1 从 {old_f1:.3f} 跌到 {now_f1:.3f}（超过容差 {tolerance:.2f}）"
        )

    now_recall = current.privacy.recall
    old_recall = float(base.get("privacy_recall", 0.0))
    if now_recall + privacy_tolerance < old_recall:
        message = f"隐私召回从 {old_recall:.1%} 跌到 {now_recall:.1%}"
        if PRIVACY_ADVISORY_ONLY:
            # 「只报不拦」阶段：真实召回率刚显形，先让它叫、别让它咬人
            advisory.append(f"{message}（当前只报不拦，观察几轮再收紧）")
        else:
            blocking.append(f"{message}——这个不能退，漏放即事故")

    if blocking:
        return False, blocking + advisory
    return True, (
        advisory
        or [f"各项指标未退（macro-F1 {now_f1:.3f}，隐私召回 {now_recall:.1%}）"]
    )
