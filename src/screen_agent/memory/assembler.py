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
        self.last_used_event_ids: list[str] = []

    def build_agent_context(self, goal: str, budget_tokens: int = 350) -> str:
        """给**任务规划**用的记忆片段（区别于给闲聊的 system prompt）。

        侧重完全不同：planner 要的是「**这件事你平时怎么干的**」——
        先例（episodes）比用户画像（facts）有用得多。
        「用户喜欢深色主题」对拆解「整理下载目录」毫无帮助，
        但「上次整理是按文件类型分的」直接决定这一步该怎么写。

        预算也刻意压得小（默认 350 token）：planner 的 prompt 里已经有一长串工具清单，
        记忆塞多了会把工具挤掉，那是本末倒置。

        取不到东西时返回空串，调用方据此跳过——**别让一个空标题占着 prompt**。
        """
        from datetime import datetime

        lines: list[str] = []
        used = 0

        # 先例优先：做过什么类似的事
        try:
            episodes = self.retriever.retrieve(query=goal, top_k=3)
        except Exception:  # noqa: BLE001 - 记忆取不到不该影响规划
            episodes = []
        for scored in episodes:
            summary = (scored.event.summary or "").strip().replace("\n", " ")
            if not summary:
                continue
            line = f"- 之前做过：{summary[:60]}"
            cost = _estimate_tokens(line)
            if used + cost > budget_tokens:
                break
            lines.append(line)
            used += cost

        # 还有预算才补用户偏好——它的作用是润色参数，不是决定拆解方式
        if used < budget_tokens:
            facts = self.store.list_facts(limit=self.max_facts * 2)
            facts.sort(key=lambda f: -fact_score(f, datetime.now()))
            for fact in facts:
                if used >= budget_tokens:
                    break
                if fact.confidence < 0.3:      # 与检索判分口径保持一致，别把噪声喂进去
                    continue
                line = f"- [{fact.category}] {fact.content}"
                cost = _estimate_tokens(line)
                if used + cost > budget_tokens:
                    continue
                lines.append(line)
                used += cost

        return "\n".join(lines)

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

        # 前瞻记忆：未完成的意图提醒模型（Typed Intention Store）
        try:
            pending = self.store.list_intentions(status="pending", limit=5)
        except Exception:
            pending = []
        if pending:
            lines = [f"- {it['content']}" + (f"（应于 {it['due_at'][:16]}）" if it["due_at"] else "") for it in pending]
            sections.append(("用户交代的待办/意图", "\n".join(lines)))

        episodes = self.retriever.retrieve(query=user_text, top_k=self.max_episodes)
        # 反思回填轨迹：实际进入上下文的 episodes（MemOS 式 importance 提权依据）
        self.last_used_event_ids = [s.event.event_id for s in episodes if s.event.event_id]
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
