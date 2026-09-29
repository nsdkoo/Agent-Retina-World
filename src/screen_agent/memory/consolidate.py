"""Consolidator：记忆固化（反思回路 v1）。

规则提取 + LLM 候选 hook：
- v1 规则：对话显式模式（"我叫X"/"记住X"/"我喜欢X"/"我在做X"）→ facts；
  事件高频主题（同 task_tag 累计时长 ≥ 30 分钟）→ project fact
- P1 LLM hook：extract_from_turn(..., llm_fn) 传入 chat 客户端回调，
  模型只产候选、规则裁决入库（防幻觉直写记忆）
"""

from __future__ import annotations

import re
from collections.abc import Callable

from screen_agent.activity.store import ActivityEvent
from screen_agent.memory.store import MemoryStoreV2

_NAME_PATTERNS = [
    re.compile(r"我叫([\u4e00-\u9fffA-Za-z0-9_]{1,12})"),
    re.compile(r"我的名字是([\u4e00-\u9fffA-Za-z0-9_]{1,12})"),
]
_PREF_PATTERN = re.compile(r"我(?:喜欢|常用|一般用)([^，。,.!！?？]{1,20})")
_REMEMBER_PATTERN = re.compile(r"记住[：:]?(.{1,40})")
_RENOUNCE_RE = re.compile(r"(?:不再|不喜欢|不用|卸载了?)([\u4e00-\u9fffA-Za-z0-9_]{1,16})")
_SWITCH_RE = re.compile(r"把([^，。,.!！?？]{1,12})换成([^，。,.!！?？]{1,12})")
_PROJECT_PATTERN = re.compile(r"我在做(?:一个)?([^，。,.!！?？]{1,24})(?:项目|系统|工具|助手)?")

_MINING_MIN_MINUTES = 30.0

# 梦境期 LLM 候选提取提示词：模型提议、规则裁决（防幻觉直写）
EXTRACTION_PROMPT = (
    "从下面的对话中提取关于用户的、值得长期记住的事实（身份、偏好、习惯、正在进行的项目）。{NL}"
    "每行一条，格式严格为：类别|内容{NL}"
    "类别只能是 profile / preference / project / entity 之一。{NL}"
    "只提取稳定信息；忽略闲聊、问候、临时上下文；最多 5 条；没有就只输出：无{NL}{NL}对话：{NL}{turns}"
).replace("{NL}", chr(10))
_QUESTION_RE = re.compile(r"[?？]|吗[？?]?\s*$|什么|哪些|怎么|怎么样|多少|几[点个时]|是谁|在哪")


class Consolidator:
    def __init__(self, store: MemoryStoreV2) -> None:
        self.store = store

    def extract_from_turn(
        self,
        user_text: str,
        assistant_reply: str | None = None,
        llm_fn: Callable[[str], list[str]] | None = None,
        evidence: str = "",
    ) -> int:
        """从一轮对话提取事实，返回新写入/更新的 fact 数。evidence 溯源到来源轮次/会话。
        疑问句不挖事实（"你喜欢什么"≠"你喜欢X"），只有显式「记住X」例外。"""
        written = 0
        is_question = bool(_QUESTION_RE.search(user_text))
        for pattern in _NAME_PATTERNS:
            m = pattern.search(user_text)
            if m and not is_question:
                self.store.add_fact("profile", f"用户名字是{m.group(1)}", source="chat_rule", confidence=0.9, evidence=evidence)
                written += 1
                break
        m = _PREF_PATTERN.search(user_text)
        if m and not is_question:
            self.store.add_fact("preference", f"用户喜欢/常用：{m.group(1).strip()}", source="chat_rule", confidence=0.75, evidence=evidence)
            written += 1
        m = _REMEMBER_PATTERN.search(user_text)
        if m:
            self.store.add_fact("entity", m.group(1).strip(), source="chat_rule", confidence=0.8, evidence=evidence)
            written += 1
        m = _PROJECT_PATTERN.search(user_text)
        if m and not is_question:
            self.store.add_fact("project", f"用户在做：{m.group(1).strip()}", source="chat_rule", confidence=0.7, evidence=evidence)
            written += 1
        # 显式放弃/更换：新 fact 入库 + 旧 fact 降级（supersede）
        m = _RENOUNCE_RE.search(user_text)
        if m and not is_question:
            old_term = m.group(1).strip()
            for prefix in ("喜欢", "用"):
                if old_term.startswith(prefix) and len(old_term) > len(prefix):
                    old_term = old_term[len(prefix):]
            old_term = old_term.rstrip("了").strip()
            if old_term:
                self.store.add_fact(
                    "preference", f"用户不再用/喜欢：{old_term}", source="chat_rule",
                    confidence=0.8, evidence=evidence, supersede_keyword=old_term,
                )
                written += 1
        m = _SWITCH_RE.search(user_text)
        if m and not is_question:
            old_term, new_term = m.group(1).strip(), m.group(2).strip()
            self.store.add_fact(
                "preference", f"用户改用：{new_term}", source="chat_rule",
                confidence=0.85, evidence=evidence, supersede_keyword=old_term,
            )
            written += 1

        # P1 LLM hook：模型产候选（"category|content" 行），规则校验入库
        if llm_fn is not None:
            try:
                candidates = llm_fn(user_text)
            except Exception:
                candidates = []
            for line in candidates:
                if "|" not in line:
                    continue
                category, content = (part.strip() for part in line.split("|", 1))
                if category in ("profile", "preference", "project", "entity") and 0 < len(content) <= 80:
                    self.store.add_fact(category, content, source="chat_llm", confidence=0.55, evidence=evidence)
                    written += 1
        return written

    def mine_projects_from_events(self, events: list[ActivityEvent]) -> int:
        """事件层高频主题 → project fact（同 task_tag 累计时长 ≥ 30 分钟）。"""
        totals: dict[str, float] = {}
        for e in events:
            if not e.task_tag:
                continue
            totals[e.task_tag] = totals.get(e.task_tag, 0.0) + e.duration_seconds()
        written = 0
        for tag, seconds in totals.items():
            if seconds / 60.0 < _MINING_MIN_MINUTES:
                continue
            sample = next((e for e in events if e.task_tag == tag), None)
            if sample is None:
                continue
            content = f"近期高频活动：{sample.page_category}/{sample.user_action}（{sample.summary[:40]}）"
            self.store.add_fact("project", content, source="event_mining", confidence=0.6, evidence="event_mining")
            written += 1
        return written
