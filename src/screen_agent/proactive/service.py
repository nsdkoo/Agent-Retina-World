from __future__ import annotations

from datetime import date, datetime
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

        lines = [f"# {day.isoformat()} 每日总结", "", "## 活动概览", ""]
        for e in events:
            mins = e.duration_seconds() / 60
            lines.append(f"- **{e.started_at.strftime('%H:%M')}–{e.ended_at.strftime('%H:%M')}** "
                         f"| {e.page_category} | {e.user_action} | {mins:.1f}min")
            lines.append(f"  - {e.summary}")

        lines.extend(["", "## 时间分布", ""])
        totals = self.memory.time_by_category()
        for cat, sec in sorted(totals.items(), key=lambda x: -x[1]):
            lines.append(f"- {cat}: {sec / 60:.1f} 分钟")

        text = "\n".join(lines)
        out = self.output_dir / f"daily_{day.isoformat()}.md"
        out.write_text(text, encoding="utf-8")
        return text

    def todo_suggestions(self) -> list[str]:
        events = self.memory.list_events(limit=20)
        todos: list[str] = []
        for e in events:
            if e.user_action in ("debugging", "typing") and "mock" not in e.summary:
                todos.append(f"跟进：{e.summary}（证据 {len(e.evidence_paths)} 张截图）")
        if not todos:
            todos.append("暂无自动推断待办，可手动补充。")
        return todos

    def _events_on(self, day: date) -> list[ActivityEvent]:
        events = self.memory.list_events(limit=200)
        return [e for e in events if e.started_at.date() == day]
