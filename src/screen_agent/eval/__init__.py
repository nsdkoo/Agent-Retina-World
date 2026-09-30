"""评测与数据飞轮：黄金集、指标、运行器、候选池。

    from screen_agent.eval import GoldenSet, Evaluator, Flywheel

    golden = GoldenSet.seed()
    report = Evaluator(classifier=clf, privacy=gate).run(golden)
    print(report.summary())
"""

from screen_agent.eval.dataset import (
    ACTIVITIES,
    POOLS,
    ActivityCase,
    GoldenSet,
    PrivacyCase,
)
from screen_agent.eval.flywheel import Candidate, Flywheel
from screen_agent.eval.metrics import (
    ClassificationReport,
    GateReport,
    classification_report,
    cohens_kappa,
    gate_report,
)
from screen_agent.eval.runner import Evaluator, EvalReport, compare_to_baseline

__all__ = [
    "ACTIVITIES", "POOLS", "ActivityCase", "Candidate", "ClassificationReport",
    "EvalReport", "Evaluator", "Flywheel", "GateReport", "GoldenSet", "PrivacyCase",
    "classification_report", "cohens_kappa", "compare_to_baseline", "gate_report",
]
