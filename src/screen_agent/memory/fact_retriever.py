"""事实级检索：按「查询相关度 × 事实权重」给 Fact 排序。

**为什么要单独一个**：现成的 `HybridRetriever` 检索的是**事件**
（`ActivityEvent`，屏幕活动记录），而画像层要检索的是**事实**
（`Fact`，用户是谁 / 喜欢什么 / 在做什么项目）。两者对象不同，衰减逻辑也不同：

- 事件按**时间**衰减（半衰期 48h）
- 事实按 **confidence × 类型条件衰减**：偏好会随时间过期，身份不会
  （`FACT_HALF_LIFE_DAYS` 里 profile 衰得慢、entity 衰得快）

拿事件检索器去测事实检索，测的其实是别的东西——这正是画像层「检索」一直是假的原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from screen_agent.memory.retriever import overlap_of, tokens_of
from screen_agent.memory.store import Fact, MemoryStoreV2, fact_decay, fact_score


@dataclass
class ScoredFact:
    fact: Fact
    score: float
    relevance: float        # bigram 词元重叠——用于排序
    char_relevance: float   # 单字覆盖率——用于门槛
    decay: float            # 类型条件衰减后的保留权重
    confidence: float


_CJK_RANGE = ("\u4e00", "\u9fff")


def query_char_coverage(query: str, content: str) -> float:
    """查询里的中文字有多少出现在了事实里。

    **为什么排序用 bigram、门槛却要单字**：bigram 对短句太稀疏——
    「我之前说的那个项目」切成 `个项`，而事实里写的是「用户在做…」，
    语义相关但一个词元都不重叠（实测 m08/m15 的相关事实 relevance 全是 0.0）。
    反过来，「今天中午吃什么」跟任何记忆都不搭，单字覆盖率也确实是 0。
    所以单字覆盖率恰好能把这两种情况分开——它只看"字面上有没有共同的字"。
    """
    q = {ch for ch in query if _CJK_RANGE[0] <= ch <= _CJK_RANGE[1]}
    c = {ch for ch in content if _CJK_RANGE[0] <= ch <= _CJK_RANGE[1]}
    if not q or not c:
        return 0.0
    return len(q & c) / len(q)


class FactRetriever:
    """给一句查询找相关的事实。"""

    def __init__(self, store: MemoryStoreV2, now: datetime | None = None) -> None:
        self.store = store
        self.now = now or datetime.now()

    def retrieve(
        self, query: str = "", top_k: int = 5, min_relevance: float = 0.05
    ) -> list[ScoredFact]:
        """按相关度排序返回事实。

        有查询时：`0.7·相关度 + 0.3·事实权重`——相关度为主，但让高置信度、新鲜的事实
        在同等相关度下排前面。
        没有查询时：纯按事实权重，相当于「现在最要紧的那几件事」。

        **`min_relevance` 是必要的门槛**：不设的话，一句跟记忆完全无关的话
        （"今天中午吃什么"）也会把 top-k 倒出来——因为相关度是 0、但事实权重不是 0，
        排序照样排得上。画像是要塞进 prompt 的，宁可不给也不能给噪音。
        默认 0.05 是个很低的门槛：只要有词元重叠就返回。

        分词口径复用事件检索器的 `tokens_of`：**两处必须一致**，
        否则「同一句话在两个检索器里算出不同的相关度」这种问题极难查。
        """
        facts = self.store.list_facts(limit=200)
        if not facts:
            return []

        query_tokens = tokens_of(query) if query else set()
        scored: list[ScoredFact] = []
        for fact in facts:
            relevance = (
                overlap_of(query_tokens, tokens_of(fact.content)) if query_tokens else 0.0
            )
            char_rel = query_char_coverage(query, fact.content) if query else 0.0
            weight = fact_score(fact, self.now)
            # 排序仍以 bigram 相关度为主（它更能反映"说得是不是同一件事"），
            # 单字覆盖率只用来当门槛——见 query_char_coverage 的说明
            score = (relevance * 0.7 + weight * 0.3) if query_tokens else weight
            scored.append(ScoredFact(
                fact=fact,
                score=score,
                relevance=relevance,
                char_relevance=char_rel,
                decay=fact_decay(fact, self.now),
                confidence=fact.confidence,
            ))
        if query and min_relevance > 0:
            scored = [item for item in scored if item.char_relevance >= min_relevance]
        scored.sort(key=lambda item: -item.score)
        return scored[:top_k]
