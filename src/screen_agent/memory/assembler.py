"""ContextAssembler：记忆 → chat system prompt 的三段式装配。

[语义层 facts top-M] + [情节层 相关 episodes top-K] + [工作层 最近轮次]，token 预算感知。
粗略换算：CJK 1 字 ≈ 1 token，英文 ≈ 4 字符/token（离线够用，P1 换真 tokenizer）。
"""

from __future__ import annotations

from screen_agent.memory.retriever import HybridRetriever, format_for_prompt
from screen_agent.memory.store import MemoryStoreV2, fact_score


def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(other // 4, 1)


class ContextAssembler:
    def __init__(
        self,
        store: MemoryStoreV2,
        retriever: HybridRetriever | None = None,
        budget_tokens: int = 1200,
        max_facts: int = 8,
        max_episodes: int = 5,
    ) -> None:
        self.store = store
        self.retriever = retriever or HybridRetriever(store)
        self.budget_tokens = budget_tokens
        self.max_facts = max_facts
        self.max_episodes = max_episodes

    def build_system_prompt(self, base_prompt: str, user_text: str | None = None) -> str:
        sections: list[tuple[str, str]] = []

        from datetime import datetime

        facts = self.store.list_facts(limit=self.max_facts * 3)
        # 类型条件衰减排序（ScrubJay）：confidence × decay，取 top-M
        facts.sort(key=lambda f: -fact_score(f, datetime.now()))
        facts = facts[: self.max_facts]
        if facts:
            fact_lines = [
                f"- [{f.category}] {f.content}（置信 {f.confidence:.1f}）" for f in facts
            ]
            sections.append(("用户记忆", "\n".join(fact_lines)))

        episodes = self.retriever.retrieve(query=user_text, top_k=self.max_episodes)
        if episodes:
            sections.append(("相关屏幕活动", format_for_prompt(episodes)))

        parts = [base_prompt, ""]
        used = _estimate_tokens(base_prompt)
        for title, body in sections:
            block = f"## {title}\n{body}"
            cost = _estimate_tokens(block)
            if used + cost > self.budget_tokens:
                continue  # 预算内装不下就跳过该层（保 base prompt）
            parts.append(block)
            parts.append("")
            used += cost
        return "\n".join(parts).strip()
