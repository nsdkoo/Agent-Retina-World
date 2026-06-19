from __future__ import annotations

from datetime import date
from pathlib import Path

from screen_agent.activity.store import ActivityEvent, MemoryStore


class ProactiveService:
    """基于活动知识库的主动服务：每日总结、待办、时间统计。"""

    def __init__(self, memory: MemoryStore, output_dir: Path) -> None:
        self.memory = memory
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def daily_summary(self, day: date | None = None) -> str:
        day = day or date.today()
        events = self._events_on(day)
        if not events:
            return f"# {day.isoformat()} 每日总结\n\n暂无活动记录。"

        lines = [
            f"# {day.isoformat()} 每日总结",
            "",
            "## 活动概览",
            "",
            "| 时段 | 场景 | 行为 | 时长 | 帧数 | 摘要 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for e in events:
            mins = e.duration_seconds() / 60
            lines.append(
                f"| {e.started_at.strftime('%H:%M')}–{e.ended_at.strftime('%H:%M')} "
                f"| {e.page_category} | {e.user_action} | {mins:.1f}min "
                f"| {e.frame_count} | {e.summary[:40]} |"
            )

        lines.extend(["", "## 时间分布", ""])
        day_totals: dict[str, float] = {}
        for e in events:
            day_totals[e.page_category] = day_totals.get(e.page_category, 0.0) + e.duration_seconds()
        for cat, sec in sorted(day_totals.items(), key=lambda x: -x[1]):
            bar = "█" * min(int(sec / 60), 20)
            lines.append(f"- **{cat}**: {sec / 60:.1f} 分钟 {bar}")

        text = "\n".join(lines)
        out = self.output_dir / f"daily_{day.isoformat()}.md"
        out.write_text(text, encoding="utf-8")
        return text

    def todo_suggestions(self) -> list[str]:
        events = self.memory.list_events(limit=30)
        todos: list[str] = []
        seen: set[str] = set()
        for e in events:
            if e.user_action not in ("debugging", "typing", "browsing"):
                continue
            key = e.task_tag or e.summary
            if key in seen:
                continue
            seen.add(key)
            todos.append(
                f"跟进 [{e.page_category}] {e.summary}（{e.frame_count} 帧 / {len(e.evidence_paths)} 证据）"
            )
        if not todos:
            todos.append("暂无自动推断待办，继续采集或接入 VLM 以提升识别精度。")
        return todos

    def timeline_markdown(self, limit: int = 20) -> str:
        events = self.memory.list_events(limit=limit)
        if not events:
            return "暂无活动时间线。"
        lines = ["# 活动时间线", ""]
        for e in reversed(events):
            lines.append(
                f"- `{e.started_at.strftime('%m-%d %H:%M')}` "
                f"**{e.page_category}** · {e.user_action} · {e.summary}"
            )
        return "\n".join(lines)

    def _events_on(self, day: date) -> list[ActivityEvent]:
        events = self.memory.list_events(limit=200)
        return [e for e in events if e.started_at.date() == day]
