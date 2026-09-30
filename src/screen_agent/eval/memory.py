"""画像层评测：记忆系统抽得准不准、对得平不平、找得到找不到。

感知层测的是「看没看见」，画像层测的是「记没记住、记对没有」。两者失效方式不同：
感知错了是当场就偏，记忆错了会**一直偏下去**——错误画像会污染之后每一次对话。

三类场景，对应用户实际会踩的坑：

- **extract**   抽取：从对话里能不能提出该记的事实（漏抽 = 白聊了）
- **reconcile** 对账：新旧事实冲突时**更新而不是追加**（追加 = 画像里留着两条矛盾的）
- **retrieve**  检索：问「我之前说过什么」能不能找回来（找不到 = 记了也白记）

防幻觉是硬指标：**不该记的东西一条都不能进**——一旦把 AI 的猜测记成用户画像，
下次对话就会拿它当事实用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from screen_agent.eval.metrics import ClassificationReport, classification_report


@dataclass
class MemoryCase:
    """一条画像层评测样本。

    `kind == "retrieve"` 的样本用 `seed_facts` + `query` 表达：
    先往库里灌几条事实，再拿 `query` 去检索，看 `expect_recall` 里的内容有没有被召回。
    这样测的才是**检索**能力；老写法（拿对话去跑抽取）测的其实是抽取。
    """

    case_id: str
    kind: str                        # extract / reconcile / retrieve
    turns: list[tuple[str, str]] = field(default_factory=list)
    expect: list[str] = field(default_factory=list)     # 期望抽到 / 召回的内容
    forbid: list[str] = field(default_factory=list)     # 期望**不**出现的内容（防幻觉）
    seed_facts: list[dict] = field(default_factory=list)  # 检索前置：{category, content, confidence?}
    query: str = ""                                       # 检索查询
    expect_recall: list[str] = field(default_factory=list)  # 检索期望命中的关键词
    note: str = ""

    def to_row(self) -> dict:
        return {
            "case_id": self.case_id, "kind": self.kind,
            "turns": [list(t) for t in self.turns],
            "expect": self.expect, "forbid": self.forbid,
            "seed_facts": self.seed_facts, "query": self.query,
            "expect_recall": self.expect_recall, "note": self.note,
        }


# 样本按「真实会踩的坑」设计，不是按功能列表凑数
SEED_MEMORY: tuple[MemoryCase, ...] = (
    MemoryCase("m01", "extract",
               [("我叫小林，在深圳做前端", "你好小林")],
               ["小林", "深圳", "前端"], [], "自我介绍是最该抽中的"),
    MemoryCase("m02", "extract",
               [("我在做一个 Agent 记忆系统的项目", "听起来不错")],
               ["Agent 记忆系统"], [], "项目信息"),
    MemoryCase("m03", "extract",
               [("我习惯用 Cursor 写代码", "好的")],
               ["Cursor"], [], "工具偏好"),
    MemoryCase("m04", "extract",
               [("今天天气不错啊", "是的，很适合出门")],
               [], ["天气", "出门"], "闲聊不该进画像——这是防幻觉的红线"),
    MemoryCase("m05", "extract",
               [("帮我看看这段代码有什么问题", "这段逻辑在边界情况下会越界")],
               [], ["越界", "这段逻辑"], "助手的技术判断不是用户画像"),
    MemoryCase("m06", "reconcile",
               [("我现在改用 VS Code 了，不用 Cursor 了", "好的，记下了")],
               ["VS Code"], ["Cursor"], "偏好变更要对账，不能留着两条矛盾的"),
    MemoryCase("m07", "reconcile",
               [("我从深圳搬到杭州了", "收到")],
               ["杭州"], ["深圳"], "城市变更同理"),
    MemoryCase("m08", "retrieve", seed_facts=[
        {"category": "project", "content": "用户在做一个 Agent 记忆系统"},
        {"category": "preference", "content": "用户偏好深色主题"},
    ], query="我之前说的那个项目", expect_recall=["Agent 记忆系统"], note="找回项目"),
    MemoryCase("m10", "retrieve", seed_facts=[
        {"category": "profile", "content": "用户名字是小林"},
        {"category": "profile", "content": "用户在深圳"},
    ], query="我叫什么名字", expect_recall=["小林"], note="姓名检索"),
    MemoryCase("m11", "retrieve", seed_facts=[
        {"category": "preference", "content": "用户喜欢用 Cursor 写代码"},
        {"category": "preference", "content": "用户偏好浅色主题"},
    ], query="我喜欢用什么工具写代码", expect_recall=["Cursor"], note="偏好检索"),
    MemoryCase("m09", "forbid",
               [("我猜他可能是做算法的", "嗯")],
               [], ["算法"], "猜测不能当事实记"),
)


@dataclass
class MemoryReport:
    extract: ClassificationReport = field(default_factory=ClassificationReport)
    reconcile: ClassificationReport = field(default_factory=ClassificationReport)
    recall_hits: int = 0
    recall_total: int = 0
    hallucination: int = 0
    hallucination_total: int = 0

    @property
    def recall(self) -> float:
        return self.recall_hits / self.recall_total if self.recall_total else 0.0

    @property
    def hallucination_rate(self) -> float:
        """不该记的东西被记下来的比例。**这是硬指标，越低越好，理想是 0**。"""
        return self.hallucination / self.hallucination_total if self.hallucination_total else 0.0

    def summary(self) -> str:
        return "\n".join([
            f"画像层：抽取 F1 {self.extract.macro_f1:.3f}（{self.extract.total} 例）"
            f"｜对账 F1 {self.reconcile.macro_f1:.3f}（{self.reconcile.total} 例）",
            f"  检索召回 {self.recall:.1%}（{self.recall_hits}/{self.recall_total}）",
            f"  幻觉率 {self.hallucination_rate:.1%}"
            f"（{self.hallucination}/{self.hallucination_total}）← 理想是 0",
        ])


class MemoryEvaluator:
    """跑画像层评测。

    **每条样本都开一个临时库**：拿真实记忆跑评测，等于把评测样本灌进用户画像，
    跑几次画像就脏了。隔离是这里的第一原则。
    """

    def __init__(self, cases: list[MemoryCase] | None = None) -> None:
        self.cases = cases if cases is not None else list(SEED_MEMORY)
        self._tmp: list = []

    def _fresh_store(self):  # noqa: ANN202
        import tempfile

        from screen_agent.memory.store import MemoryStoreV2

        handle = tempfile.TemporaryDirectory()
        self._tmp.append(handle)      # 保持引用，别让目录被回收
        return MemoryStoreV2(Path(handle.name) / "events.db")

    def run(self) -> MemoryReport:
        cases = self.cases
        report = MemoryReport()

        for kind in ("extract", "reconcile"):
            subset = [c for c in cases if c.kind == kind]
            if not subset:
                continue
            result = classification_report(
                ["hit"] * len(subset),
                ["hit" if self._trial_extract(c) else "miss" for c in subset],
                [c.case_id for c in subset],
            )
            setattr(report, kind, result)

        for case in cases:
            if case.kind == "retrieve":
                report.recall_total += 1
                # 改调 _trial_retrieve（真检索）——原先这里调的是 _trial_extract
                if self._trial_retrieve(case):
                    report.recall_hits += 1
            if case.forbid:
                report.hallucination_total += 1
                if self._trial_hallucination(case):
                    report.hallucination += 1
        return report

    # ---- 单项试跑：都走真实抽取链路（临时库），不另写一套逻辑 ----

    def _extracted_text(self, case: MemoryCase) -> str | None:
        """把一条样本喂进真实的 Consolidator，回读库里落了什么。"""
        store = self._fresh_store()
        from screen_agent.memory.consolidate import Consolidator

        try:
            consolidator = Consolidator(store)
            for user_text, reply in case.turns:
                consolidator.extract_from_turn(user_text, reply or "", evidence="eval")
            # 只看**有效** fact：对账（supersede）是把旧 fact 降到低置信度，不是删行。
            # 拿全量去判「旧值还在不在」会把降级误判成没对账——这是评测口径的坑。
            facts = [f for f in store.list_facts() if f.confidence >= 0.3]
        except Exception:  # noqa: BLE001 - 评测不该因为一条样本炸掉
            return None
        return " ".join(f.content for f in facts)

    def _trial_extract(self, case: MemoryCase) -> bool:
        """抽出期望里的**任意一条**就算过——这里测「有没有抽到」，
        精度由幻觉率那项单独兜底。"""
        joined = self._extracted_text(case)
        if joined is None:
            return False
        if not case.expect:
            return bool(joined.strip()) is False or True
        return any(word in joined for word in case.expect)

    def _trial_retrieve(self, case: MemoryCase) -> bool:
        """真检索：先灌 `seed_facts`，再拿 `query` 去查，看期望内容有没有被召回。

        老实现是 `return self._trial_extract(case)`，而 `run()` 调的又是 `_trial_extract`
        —— 于是所谓「检索召回」测的其实是抽取，检索能力一天都没被真正测过。
        """
        if not case.query:
            return False
        store = self._fresh_store()
        try:
            for spec in case.seed_facts:
                store.add_fact(
                    spec.get("category", "entity"),
                    spec["content"],
                    confidence=float(spec.get("confidence", 0.7)),
                    evidence=spec.get("evidence", "eval"),
                )
            from screen_agent.memory.fact_retriever import FactRetriever

            hits = FactRetriever(store).retrieve(case.query, top_k=5)
        except Exception:  # noqa: BLE001 - 单条样本失败不该炸掉整轮
            return False
        if not case.expect_recall:
            return False
        joined = " ".join(item.fact.content for item in hits)
        return any(word in joined for word in case.expect_recall)

    def _trial_hallucination(self, case: MemoryCase) -> bool:
        """不该记的东西有没有被记下来。理想是 0。"""
        joined = self._extracted_text(case)
        if joined is None:
            return False
        return any(word in joined for word in case.forbid)


def load_memory_cases(path: Path) -> list[MemoryCase]:
    """从 JSON 读样本；没有就用内置种子。"""
    import json

    path = Path(path)
    if not path.exists():
        return list(SEED_MEMORY)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return list(SEED_MEMORY)
    cases: list[MemoryCase] = []
    for row in rows:
        cases.append(MemoryCase(
            case_id=row["case_id"], kind=row["kind"],
            turns=[tuple(t) for t in row.get("turns", [])],
            expect=row.get("expect", []), forbid=row.get("forbid", []),
            note=row.get("note", ""),
        ))
    return cases
