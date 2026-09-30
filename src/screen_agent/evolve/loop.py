"""自进化回路：生成候选改进 → 外部评测 → 胜出才采纳 → 记血缘。

对标 **Darwin Gödel Machine**（Sakana AI + UBC，ICLR 2026）的四步：

    generate changes → evaluate externally → retain diverse ancestors → audit failures

DGM 那道题的边界值得抄清楚：**它改的是 agent 脚手架，模型权重全程冻结**，
而且每次改动都必须过外部基准验证。本项目同样——改的是关键词规则与阈值，
分类模型不动。

两条从 DGM 的教训里直接搬过来的纪律：

1. **奖励黑客真实发生过**。DGM 有个子代不是提升能力，而是**绕过了检测器**来把分数做上去。
   所以这里的候选改进**看不到留出集（challenge 池）**，验证必须跑在它没见过的样本上。
   看得见的分数能被优化，看不见的才叫验证。

2. **多样性比只留最优更重要**。DGM 消融实验：去掉开放探索只剩 23%（完整版 50%）。
   所以档案库保留**整棵树 + 血缘**，不是只留冠军——下一步的变异可能从任何节点出发。
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from screen_agent.eval.metrics import classification_report
from screen_agent.eval.runner import Evaluator
from screen_agent.eval.dataset import GoldenSet

# 采纳门槛：开发集上必须有真实提升，且留出集不许跌
MIN_DEV_GAIN = 0.0
HOLDOUT_TOLERANCE = 0.0


@dataclass
class Variant:
    """一个候选改进，同时也是血缘树上的一个节点。"""

    variant_id: str
    parent_id: str = ""
    kind: str = "rules"            # rules / threshold
    payload: dict = field(default_factory=dict)
    rationale: str = ""
    dev_f1: float = 0.0
    holdout_f1: float = 0.0
    holdout_privacy: float = 1.0
    accepted: bool = False
    reason: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def summary(self) -> str:
        mark = "采纳" if self.accepted else "拒绝"
        return (
            f"[{mark}] {self.variant_id} ({self.kind}) "
            f"dev-F1 {self.dev_f1:.3f} / holdout-F1 {self.holdout_f1:.3f} "
            f"/ 隐私召回 {self.holdout_privacy:.1%}　{self.reason}"
        )


class Archive:
    """档案库：留整棵树，不只是冠军。DGM 的消融证明多样性本身就值 27 个百分点。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._variants: dict[str, Variant] = {}
        self._load()

    def add(self, variant: Variant) -> None:
        self._variants[variant.variant_id] = variant
        self._save()

    def get(self, variant_id: str) -> Variant | None:
        return self._variants.get(variant_id)

    def accepted(self) -> list[Variant]:
        return [v for v in self._variants.values() if v.accepted]

    def best(self) -> Variant | None:
        pool = self.accepted()
        return max(pool, key=lambda v: v.holdout_f1) if pool else None

    def lineage(self, variant_id: str) -> list[str]:
        """回溯血缘：这个改动是从哪条链上变异出来的。出问题要能一路查回去。"""
        chain: list[str] = []
        cursor = self._variants.get(variant_id)
        while cursor is not None:
            chain.append(cursor.variant_id)
            cursor = self._variants.get(cursor.parent_id) if cursor.parent_id else None
        return chain

    def next_id(self, offset: int = 0) -> str:
        """下一个可用 ID。生成候选时用 offset 预留位次——那会儿还没入库，
        不偏移的话一批候选会全叫 v001。"""
        return f"v{len(self._variants) + offset + 1:03d}"

    def stats(self) -> dict:
        return {
            "total": len(self._variants),
            "accepted": len(self.accepted()),
            "rejected": len(self._variants) - len(self.accepted()),
        }

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for row in rows:
            try:
                variant = Variant(**row)
            except TypeError:
                continue
            self._variants[variant.variant_id] = variant

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([asdict(v) for v in self._variants.values()],
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


class Evolver:
    """一轮进化：提候选 → 开发集评 → 留出集验 → 达标才采纳。"""

    def __init__(
        self,
        evaluator: Evaluator,
        golden: GoldenSet,
        archive: Archive | None = None,
        rules: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.evaluator = evaluator
        self.golden = golden
        self.archive = archive or Archive()
        self.rules = rules if rules is not None else self._load_rules()

    @staticmethod
    def _load_rules() -> dict[str, tuple[str, ...]]:
        from screen_agent.understand import classify

        return {activity: tuple(hints) for activity, hints in classify._RULES}

    # ---- 生成候选 ----

    def propose_from_failures(self, failures: list[tuple[str, str, str]],
                              limit: int = 4) -> list[Variant]:
        """按目标类别聚合失败案例，每类生成一个候选（可含多个关键词）。

        逐个案例生成太保守——同一类往往一次错好几条，一条条修效率太低，
        而且每条只加一个词，验证时几乎看不出提升（实测就是这样：14 个错判只采纳了 1 项）。
        按 `expect` 分组后，一个候选就能把该类的高频关键词一起补上。

        聚合还有一层好处：**一次改动解决一类问题**，语义清楚、回滚也干净。
        """
        grouped: dict[str, list[str]] = {}
        for case_id, expect, _got in failures:
            grouped.setdefault(expect, []).append(case_id)

        proposals: list[Variant] = []
        for index, (expect, case_ids) in enumerate(list(grouped.items())[:limit]):
            keywords: list[str] = []
            for case_id in case_ids:
                case = self.golden.by_id(case_id)
                if case is None:
                    continue
                keyword = _pick_keyword(case.text, case.window_title)
                if keyword and keyword not in keywords:
                    keywords.append(keyword)
            current = tuple(self.rules.get(expect, ()))
            fresh = [k for k in keywords if k not in current]
            if not fresh:
                continue
            proposals.append(Variant(
                variant_id=self.archive.next_id(offset=index),
                parent_id=(self.archive.best().variant_id if self.archive.best() else ""),
                kind="rules",
                payload={"activity": expect, "keywords": fresh},
                rationale=(
                    f"{expect} 类错了 {len(case_ids)} 条，"
                    f"补关键词 {'、'.join(fresh[:4])}"
                ),
            ))
        return proposals

    # ---- 评测与验证 ----

    def score_dev(self, rules: dict) -> tuple[float, float]:
        return self._score(rules, self.golden.dev())

    def score_holdout(self, rules: dict) -> tuple[float, float]:
        return self._score(rules, self.golden.holdout())

    def _score(self, rules: dict, cases: list) -> tuple[float, float]:
        """用一组规则跑一批样本，返回 (macro-F1, 隐私召回)。

        打分靠**临时替换规则**再走一遍现有分类器，不改任何全局状态——
        评测过程必须是只读的，否则下一次评测就被上一次污染了。
        """
        from screen_agent.understand import classify

        original = classify._RULES
        try:
            classify._RULES = tuple(
                (name, tuple(rules.get(name, ()))) for name in classify.ACTIVITY_LABELS
            )
            truths, preds = [], []
            for case in cases:
                if not hasattr(case, "expect"):
                    continue
                # 直接调规则判定，**别**走 ActivityClassifier.classify——
                # 那个方法的 enabled=False 是「关掉这个功能」的语义，会直接返回空标签，
                # 拿它来评测会让所有样本都判成 other，分数恒为 0（踩过）
                label = classify.ActivityClassifier._classify_rules(
                    f"{getattr(case, 'app', '')} "
                    f"{getattr(case, 'window_title', '')} {case.text}"
                )
                truths.append(case.expect)
                preds.append(label.activity)
        finally:
            classify._RULES = original

        report = classification_report(truths, preds)
        privacy = 1.0
        if self.evaluator.privacy is not None:
            expected, predicted = [], []
            for case in self.golden.privacy:
                allowed, _ = self.evaluator.privacy.verdict(
                    case.title, case.process, case.texts
                )
                # 期望取自黄金集标注，**不能拿判定结果自己当期望**——那样 expected 恒等于
                # predicted，漏放数永远 0、recall 永远 1.0，第三条采纳红线形同虚设（踩过）
                expected.append(bool(getattr(case, "expect_blocked", False)))
                predicted.append(not allowed)
            from screen_agent.eval.metrics import gate_report

            privacy = gate_report(expected, predicted).recall
        return report.macro_f1, privacy

    def base_dev_f1(self) -> float:
        return self.score_dev(self.rules)[0]

    def verify(self, variant: Variant, base_dev: float, base_holdout: float,
               base_privacy: float) -> Variant:
        """开发集评 + 留出集验。两条都要过才采纳。

        - 开发集：必须**真的有提升**（不是持平）
        - 留出集：**不许跌**——这就是防过拟合和防奖励黑客的那道闸
        - 隐私召回：硬红线，跌一点就毙
        """
        candidate_rules = copy.deepcopy(self.rules)
        activity = variant.payload.get("activity")
        keywords = list(variant.payload.get("keywords") or [])
        if activity and keywords:
            candidate_rules[activity] = tuple(keywords) + tuple(
                candidate_rules.get(activity, ())
            )

        dev_f1, _ = self.score_dev(candidate_rules)
        holdout_f1, holdout_privacy = self.score_holdout(candidate_rules)

        variant.dev_f1 = dev_f1
        variant.holdout_f1 = holdout_f1
        variant.holdout_privacy = holdout_privacy

        reasons: list[str] = []
        if dev_f1 <= base_dev + MIN_DEV_GAIN:
            reasons.append(f"开发集没提升（{base_dev:.3f} → {dev_f1:.3f}）")
        if holdout_f1 + HOLDOUT_TOLERANCE < base_holdout:
            reasons.append(f"留出集退化（{base_holdout:.3f} → {holdout_f1:.3f}）")
        if holdout_privacy + HOLDOUT_TOLERANCE < base_privacy:
            reasons.append(f"隐私召回退化（{base_privacy:.1%} → {holdout_privacy:.1%}）")

        variant.accepted = not reasons
        variant.reason = "；".join(reasons) if reasons else "开发集提升且留出集未退"
        self.archive.add(variant)
        return variant

    def step(self, failures: list[tuple[str, str, str]]) -> list[Variant]:
        """跑一轮：把所有候选都验一遍，把通过的接进当前规则。

        注意**逐个验证**而不是批量替换：一个候选过了才影响下一个的基线，
        否则几个各降一点点的候选会互相掩盖。
        """
        variants = self.propose_from_failures(failures)
        results: list[Variant] = []
        for variant in variants:
            base_dev = self.base_dev_f1()
            base_holdout, base_privacy = self.score_holdout(self.rules)
            verified = self.verify(variant, base_dev, base_holdout, base_privacy)
            if verified.accepted:
                activity = verified.payload["activity"]
                keywords = list(verified.payload.get("keywords") or [])
                self.rules[activity] = tuple(keywords) + tuple(
                    self.rules.get(activity, ())
                )
            results.append(verified)
        return results


def _pick_keyword(text: str, window_title: str) -> str:
    """从错判样本里挑一个能代表它的关键词。

    优先从窗口标题里挑（标题的信噪比高），挑不到再退到正文。
    只取长度 >= 2 的片段，避免把单个字符塞进规则里造成大面积误判。
    """
    candidates: list[str] = []
    for chunk in (window_title or "").replace("—", "-").replace("|", "-").split("-"):
        clean = chunk.strip()
        if 2 <= len(clean) <= 12:
            candidates.append(clean.lower())
    if not candidates:
        for token in (text or "").split():
            clean = token.strip("，。：、,.()（）")
            if 2 <= len(clean) <= 10 and clean.isascii() is False:
                candidates.append(clean)
    return candidates[0] if candidates else ""
