"""评测指标：macro-F1、逐类指标、隐私召回、双路一致性。

**为什么主指标不是准确率**：活动类别的分布极度不均——写代码和浏览网页占绝大多数，
开会和空闲是少数。整体准确率会被大类拖着走，小类崩了也看不出来。
所以主指标用 **macro-F1**（每类等权），并且逐类给出 precision / recall。

**隐私那侧用召回率**：漏放一次是事故，误拦一次只是少记一条记录，两者的代价不对称。
拿准确率衡量隐私闸门是错的，必须盯漏放。

**双路一致性用 Cohen's Kappa**：规则判定与模型判定的一致性。
κ 低于 0.6 说明两者在按不同标准打分，这时候纠结分数高低没意义，得先对齐口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClassStats:
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    support: int = 0


@dataclass
class ClassificationReport:
    total: int = 0
    correct: int = 0
    accuracy: float = 0.0
    macro_f1: float = 0.0
    per_class: dict[str, ClassStats] = field(default_factory=dict)
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[tuple[str, str, str]] = field(default_factory=list)  # (id, 期望, 实际)

    def summary(self, title: str = "") -> str:
        head = f"{title} " if title else ""
        lines = [
            f"{head}样本 {self.total}，正确 {self.correct}，"
            f"准确率 {self.accuracy:.1%}，macro-F1 {self.macro_f1:.3f}"
        ]
        weak = sorted(
            (n, s) for n, s in self.per_class.items() if s.support
        )
        for name, stat in weak:
            flag = "  ← 弱" if stat.f1 < 0.6 and stat.support >= 2 else ""
            lines.append(
                f"  {name:<10} P {stat.precision:.2f} R {stat.recall:.2f} "
                f"F1 {stat.f1:.2f} (n={stat.support}){flag}"
            )
        if self.errors:
            lines.append(f"  错判 {len(self.errors)} 例：")
            for case_id, expect, got in self.errors[:6]:
                lines.append(f"    {case_id}: 期望 {expect} → 实际 {got}")
        return "\n".join(lines)


def classification_report(
    y_true: list[str],
    y_pred: list[str],
    case_ids: list[str] | None = None,
    labels: list[str] | None = None,
) -> ClassificationReport:
    """手算分类报告。不引 sklearn——这点体量没必要多一个依赖。"""
    ids = case_ids or [str(i) for i in range(len(y_true))]
    names = labels or sorted({*y_true, *y_pred})

    confusion: dict[str, dict[str, int]] = {a: {b: 0 for b in names} for a in names}
    for truth, pred in zip(y_true, y_pred):
        confusion.setdefault(truth, {})[pred] = confusion.setdefault(truth, {}).get(pred, 0) + 1

    per_class: dict[str, ClassStats] = {}
    for name in names:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == name and p == name)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != name and p == name)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == name and p != name)
        support = sum(1 for t in y_true if t == name)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[name] = ClassStats(precision, recall, f1, support)

    total = len(y_true)
    correct = sum(1 for t, p in zip(y_true, y_pred) if t == p)
    scored = [s.f1 for s in per_class.values() if s.support]
    errors = [
        (cid, t, p) for cid, t, p in zip(ids, y_true, y_pred) if t != p
    ]
    return ClassificationReport(
        total=total,
        correct=correct,
        accuracy=correct / total if total else 0.0,
        macro_f1=sum(scored) / len(scored) if scored else 0.0,
        per_class=per_class,
        confusion=confusion,
        errors=errors,
    )


@dataclass
class GateReport:
    """二分类门禁报告（隐私闸门、拦截类判断都用它）。"""

    total: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def recall(self) -> float:
        """漏放率 = 1 - recall。**这是隐私闸门最该盯的数**。"""
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.total if self.total else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def summary(self, title: str = "") -> str:
        head = f"{title} " if title else ""
        return (
            f"{head}样本 {self.total}　召回 {self.recall:.1%}（漏放 {self.fn}）"
            f"　精确 {self.precision:.1%}（误拦 {self.fp}）　准确 {self.accuracy:.1%}"
        )


def gate_report(y_true: list[bool], y_pred: list[bool]) -> GateReport:
    report = GateReport(total=len(y_true))
    for truth, pred in zip(y_true, y_pred):
        if truth and pred:
            report.tp += 1
        elif truth and not pred:
            report.fn += 1
        elif not truth and pred:
            report.fp += 1
        else:
            report.tn += 1
    return report


def cohens_kappa(a: list[str], b: list[str]) -> float:
    """两路评分的一致性。1 完全一致，0 等于瞎猜。

    为什么要它：规则判和模型判如果 κ 很低，说明两者压根在按不同标准打分，
    这时候比"谁分高"没意义——得先把口径对齐。
    """
    if not a or len(a) != len(b):
        return 0.0
    n = len(a)
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    labels = {*a, *b}
    expected = sum(
        (sum(1 for x in a if x == name) / n) * (sum(1 for y in b if y == name) / n)
        for name in labels
    )
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)
