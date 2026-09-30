"""HybridRetriever：recency × importance × relevance 三维打分检索。

Generative Agents 检索公式离线化：
    score = 0.4·recency + 0.3·importance + 0.3·relevance
- recency：指数衰减，半衰期 48h
- importance：写入时启发式打分（0-3），检索归一化
- relevance：CJK bigram + 英文词重叠；向量检索留接口（P1 接 embedding 配置）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from screen_agent.activity.store import ActivityEvent
from screen_agent.memory.store import MemoryStoreV2

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EN_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


@dataclass
class ScoredEvent:
    event: ActivityEvent
    score: float
    recency: float
    importance: float
    relevance: float


def _tokens(text: str) -> set[str]:
    """CJK 取 bigram，英文取小写词，混合查询与摘要的统一词元。"""
    tokens: set[str] = set()
    cjk_runs = _CJK_RE.findall(text)
    joined = "".join(cjk_runs)
    for i in range(len(joined) - 1):
        tokens.add(joined[i : i + 2])
    for word in _EN_WORD_RE.findall(text.lower()):
        if len(word) > 1:
            tokens.add(word)
    return tokens


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# 公开别名：事实级检索（fact_retriever）也用同一套分词口径。
# 两处口径不一致的话，「同一句话在两个检索器里算出不同相关度」这种问题极难查。
tokens_of = _tokens
overlap_of = _overlap


class HybridRetriever:
    W_RECENCY = 0.4
    W_IMPORTANCE = 0.3
    W_RELEVANCE = 0.3
    HALF_LIFE_HOURS = 48.0

    def __init__(
        self,
        store: MemoryStoreV2,
        now: datetime | None = None,
        vector_search=None,
    ) -> None:
        """vector_search: 可选 (query_text, top_k) -> [(event_id, score 0-1)]。
        提供时 relevance 取关键词与向量分的加权混合（SiliconFlow bge-m3 免费通道）。"""
        self.store = store
        self._now = now or datetime.now()
        self._vector_search = vector_search

    def retrieve(self, query: str | None = None, top_k: int = 5) -> list[ScoredEvent]:
        events = self.store.list_events(limit=200)
        if not events:
            return []
        query_tokens = _tokens(query) if query else set()
        vec_hits: dict[str, float] = {}
        if query and self._vector_search is not None:
            try:
                vec_hits = {
                    eid: max(0.0, min(1.0, (sc - 0.55) / 0.45))
                    for eid, sc in self._vector_search(query, 30)
                }
            except Exception:
                vec_hits = {}
        scored = [self._score(e, query_tokens, vec_hits.get(e.event_id)) for e in events]
        scored.sort(key=lambda s: -s.score)
        return scored[:top_k]

    def _score(self, event: ActivityEvent, query_tokens: set[str], vec_score: float | None = None) -> ScoredEvent:
        age_hours = max((self._now - event.ended_at).total_seconds(), 0.0) / 3600.0
        recency = 0.5 ** (age_hours / self.HALF_LIFE_HOURS)
        importance = self.store.event_importance(event.event_id) / 3.0
        relevance = (
            _overlap(query_tokens, _tokens(f"{event.summary} {event.page_category} {event.task_tag or ''}"))
            if query_tokens
            else 0.0
        )
        if vec_score is not None:
            # 语义相关与关键词相关取强者：向量能召回关键词覆盖不到的改写
            relevance = max(relevance, vec_score)
        score = (
            self.W_RECENCY * recency
            + self.W_IMPORTANCE * importance
            + self.W_RELEVANCE * relevance
        )
        return ScoredEvent(event, score, recency, importance, relevance)


def format_for_prompt(scored: list[ScoredEvent]) -> str:
    """检索结果格式化为装配器使用的一段文本。"""
    if not scored:
        return "暂无相关活动记录。"
    lines = []
    for s in scored:
        e = s.event
        lines.append(
            f"- [{e.started_at.strftime('%m-%d %H:%M')}] {e.page_category}/{e.user_action}"
            f"（{e.duration_seconds() / 60:.0f} 分钟）：{e.summary}"
        )
    return "\n".join(lines)

