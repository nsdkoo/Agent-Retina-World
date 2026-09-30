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
import random
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
        """下一个可用 ID。

        按**现有最大编号**递增，不是按条数——多轮迭代里会有加载、乱序、甚至删号的
        情况，按条数算必然撞号；一撞号 `get()` 就取到别人，血缘整个乱掉。
        """
        biggest = 0
        for variant_id in self._variants:
            if variant_id.startswith("v") and variant_id[1:].isdigit():
                biggest = max(biggest, int(variant_id[1:]))
        return f"v{biggest + offset + 1:03d}"

    # ---- 树 ----
    # 树关系只靠 parent_id 表达，不额外维护 children 字段——
    # 这样 JSON 结构不变，老档案可以直接读，反查成本也可以忽略（档案规模就几十上百个）

    def roots(self) -> list[Variant]:
        """根节点：没有父节点的变体（直接基于基础规则变异出来的）。"""
        return [v for v in self._variants.values() if not v.parent_id]

    def children(self, variant_id: str) -> list[Variant]:
        return [v for v in self._variants.values() if v.parent_id == variant_id]

    def depth(self, variant_id: str) -> int:
        """层深，根为 0。"""
        return max(0, len(self.lineage(variant_id)) - 1)

    def max_depth(self) -> int:
        return max((self.depth(v) for v in self._variants), default=0)

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
        # 基础规则是所有变异的共同起点，**永不改动**。
        # `rules_of()` 从它出发逐个重放 payload 增量，还原出任意节点的完整规则。
        self._base_rules = self._load_rules()
        self.rules = rules if rules is not None else dict(self._base_rules)

    @staticmethod
    def _load_rules() -> dict[str, tuple[str, ...]]:
        from screen_agent.understand import classify

        return {activity: tuple(hints) for activity, hints in classify._RULES}

    # ---- 血缘 → 规则 ----

    def rules_of(self, variant_id: str) -> dict[str, tuple[str, ...]]:
        """还原某个节点对应的完整规则集：基础规则 + 从根到它的每一处增量。

        **包含该节点自身的增量**（不管它有没有被采纳）——"这条配置"就是它的语义；
        `accepted` 只表示它相对父基线有没有过门禁，是两回事。

        这是让 `parent_id` 真正参与计算的关键：以前 `verify` 拿的是"当前累积规则"，
        所有候选共享同一基线，血缘纯属摆设。
        """
        rules = {name: tuple(hints) for name, hints in self._base_rules.items()}
        if not variant_id:
            return rules
        chain = list(reversed(self.archive.lineage(variant_id)))   # 根 → 叶
        for node_id in chain:
            node = self.archive.get(node_id)
            if node is None:
                continue
            activity = node.payload.get("activity")
            keywords = list(node.payload.get("keywords") or [])
            if activity and keywords:
                rules[activity] = tuple(keywords) + tuple(rules.get(activity, ()))
        return rules

    # ---- 选父节点：探索与利用 ----

    def sample_parent(
        self,
        strategy: str = "epsilon",
        epsilon: float = 0.3,
        rng: random.Random | None = None,
    ) -> str:
        """挑一个父节点来变异。返回 `""` 表示直接从基础规则下手。

        DGM 的消融实验说明这件事很值：去掉开放探索，成绩从 50% 掉到 23%。
        只从 `best()` 变异就是"没有开放探索"那一档——全档案只走一条线，
        一旦走进局部最优就再也出不来。

        - `best`   只挑已采纳里 holdout 最高的（纯利用，等价旧行为）
        - `random` 全档案均匀采样（纯探索）
        - `epsilon` ε-greedy，默认：小概率探索、大概率利用
        - `weighted` 按 `holdout_f1² / (1 + 已有子节点数)` 采样——
                     用平方拉开差距、用子节点数抑制过度开发同一分支
        """
        pool = list(self.archive._variants.values())  # noqa: SLF001 - 同模块内部使用
        if not pool:
            return ""
        rng = rng or random
        accepted = [v for v in pool if v.accepted]

        if strategy == "random":
            return rng.choice(pool).variant_id
        if strategy == "best":
            return max(accepted, key=lambda v: v.holdout_f1).variant_id if accepted else ""
        if strategy == "epsilon" and rng.random() < epsilon:
            return rng.choice(pool).variant_id          # 探索：全档案里随机挑一个
        if strategy == "weighted" and accepted:
            weights = [
                max(v.holdout_f1, 1e-6) ** 2 / (1 + len(self.archive.children(v.variant_id)))
                for v in accepted
            ]
            return rng.choices(accepted, weights=weights, k=1)[0].variant_id
        return max(accepted, key=lambda v: v.holdout_f1).variant_id if accepted else ""

    # ---- 生成候选 ----

    def propose_from_failures(self, failures: list[tuple[str, str, str]],
                              limit: int = 4, parent_id: str = "") -> list[Variant]:
        """按目标类别聚合失败案例，每类生成一个候选（可含多个关键词）。

        逐个案例生成太保守——同一类往往一次错好几条，一条条修效率太低，
        而且每条只加一个词，验证时几乎看不出提升（实测就是这样：14 个错判只采纳了 1 项）。
        按 `expect` 分组后，一个候选就能把该类的高频关键词一起补上。

        **变异基于 `parent_id` 的规则**，不是当前的累积规则——否则同一个父节点
        生出来的候选会互相看不见对方，血缘也没意义。
        """
        parent_rules = self.rules_of(parent_id)
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
            current = tuple(parent_rules.get(expect, ()))
            fresh = [k for k in keywords if k not in current]
            if not fresh:
                continue
            proposals.append(Variant(
                variant_id=self.archive.next_id(offset=index),
                parent_id=parent_id,
                kind="rules",
                payload={"activity": expect, "keywords": fresh},
                rationale=(
                    f"{expect} 类错了 {len(case_ids)} 条，"
                    f"补关键词 {'、'.join(fresh[:4])}"
                    + (f"（基于 {parent_id}）" if parent_id else "（基于基础规则）")
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

        **规则是显式参数，不碰模块全局**。以前的做法是临时改写 `classify._RULES`
        再在 finally 里还原——多轮迭代或并行评测会互相污染，中途抛异常还会把脏值
        留在全局上。评测过程必须只读，这是底线。
        """
        from screen_agent.understand import classify

        rules_tuple = tuple(
            (name, tuple(rules.get(name, ()))) for name in classify.ACTIVITY_LABELS
        )
        truths, preds = [], []
        for case in cases:
            if not hasattr(case, "expect"):
                continue
            # 走纯函数，**别**用 ActivityClassifier.classify：
            # 那个方法的 enabled=False 是「关掉这个功能」的语义，会直接返回空标签，
            # 拿它来评测会让所有样本都判成 other、分数恒为 0（踩过）
            label = classify.classify_with_rules(
                f"{getattr(case, 'app', '')} "
                f"{getattr(case, 'window_title', '')} {case.text}",
                rules_tuple,
            )
            truths.append(case.expect)
            preds.append(label.activity)

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
        # 基线取**父节点的规则**，不是当前累积规则——同一批候选共享父节点，
        # 基线就该是同一个；以前拿累积规则当基线，血缘纯属摆设
        candidate_rules = self.rules_of(variant.parent_id)
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

    def dev_failures(self, rules: dict | None = None) -> list[tuple[str, str, str]]:
        """用给定规则在**开发集**上跑一遍，返回错判 (case_id, expect, got)。

        多轮迭代要靠它重算失败——每轮采纳改进之后错误集合就变了，
        拿第一轮那份列表一直跑下去是没有意义的。

        注意只看开发集：留出集是验证用的，拿它找失败等于提前把答案看了。
        """
        from screen_agent.understand import classify

        active = rules if rules is not None else self.rules
        rules_tuple = tuple(
            (name, tuple(active.get(name, ()))) for name in classify.ACTIVITY_LABELS
        )
        failures: list[tuple[str, str, str]] = []
        for case in self.golden.dev():
            if not hasattr(case, "expect"):
                continue
            label = classify.classify_with_rules(
                f"{getattr(case, 'app', '')} "
                f"{getattr(case, 'window_title', '')} {case.text}",
                rules_tuple,
            )
            if label.activity != case.expect:
                failures.append((case.case_id, case.expect, label.activity))
        return failures

    def run(
        self,
        max_rounds: int = 5,
        patience: int = 2,
        strategy: str = "epsilon",
        epsilon: float = 0.3,
        failures: list[tuple[str, str, str]] | None = None,
        rng: random.Random | None = None,
        on_round=None,  # noqa: ANN001 - Callable[[int, list[Variant]], None]，给 CLI 打印用
    ) -> list[Variant]:
        """多轮进化：选父 → 变异 → 验证 → 迁移，跑到挖不动为止。

        跨轮只带两个状态：`self.rules`（当前最优配置）与 `self.archive`（整棵树）。

        终止条件：连续 `patience` 轮没有候选被采纳（这一带挖空了），或跑满 `max_rounds`。
        首次传入的 `failures` 只在第一轮用，之后每轮重算——改进落地后错误会变。
        """
        history: list[Variant] = []
        stagnant = 0
        for round_index in range(max(1, max_rounds)):
            fails = failures if (round_index == 0 and failures) else self.dev_failures()
            if not fails:
                break
            results = self.step(fails, strategy=strategy, epsilon=epsilon, rng=rng)
            history.extend(results)
            if on_round is not None:
                on_round(round_index + 1, results)
            if any(v.accepted for v in results):
                stagnant = 0
            else:
                stagnant += 1
                if stagnant >= max(1, patience):
                    break
        return history

    def step(self, failures: list[tuple[str, str, str]], parent_id: str | None = None,
             strategy: str = "epsilon", epsilon: float = 0.3,
             rng: random.Random | None = None) -> list[Variant]:
        """跑一轮：选父节点 → 从它变异出一批候选 → 逐个验证 → 通过的接进来。

        `parent_id=None` 时按策略自动选父；显式传值可以强行钉住某个节点。

        同一批候选**共享同一个父与同一个基线**（它们本来就是从同一处变异出来的，
        各给一条基线反而说不通）。采纳之后 `self.rules` 迁到新叶，下一轮从这里继续。
        """
        if parent_id is None:
            parent_id = self.sample_parent(strategy, epsilon, rng)
        parent_rules = self.rules_of(parent_id)
        base_dev, _ = self.score_dev(parent_rules)
        base_holdout, base_privacy = self.score_holdout(parent_rules)

        variants = self.propose_from_failures(failures, parent_id=parent_id)
        results: list[Variant] = []
        for variant in variants:
            verified = self.verify(variant, base_dev, base_holdout, base_privacy)
            if verified.accepted:
                # 采纳后迁到新叶：它代表「当前最优的那条配置」
                self.rules = self.rules_of(verified.variant_id)
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
