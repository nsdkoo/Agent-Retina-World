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
    relevance: float      # 与查询的词元重叠
    decay: float          # 类型条件衰减后的保留权重
    confidence: float


class FactRetriever:
    """给一句查询找相关的事实。"""

    def __init__(self, store: MemoryStoreV2, now: datetime | None = None) -> None:
        self.store = store
        self.now = now or datetime.now()

    def retrieve(self, query: str = "", top_k: int = 5) -> list[ScoredFact]:
        """按相关度排序返回事实。

        有查询时：`0.7·相关度 + 0.3·事实权重`——相关度为主，但让高置信度、新鲜的事实
        在同等相关度下排前面。
        没有查询时：纯按事实权重，相当于「现在最要紧的那几件事」。

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
            weight = fact_score(fact, self.now)
            score = (relevance * 0.7 + weight * 0.3) if query_tokens else weight
            scored.append(ScoredFact(
                fact=fact,
                score=score,
                relevance=relevance,
                decay=fact_decay(fact, self.now),
                confidence=fact.confidence,
            ))
        scored.sort(key=lambda item: -item.score)
        return scored[:top_k]
