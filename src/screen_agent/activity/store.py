from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from screen_agent.understand.vlm import PageUnderstanding, UserAction


@dataclass
class ActivityEvent:
    event_id: str
    started_at: datetime
    ended_at: datetime
    page_category: str
    user_action: str
    summary: str
    evidence_paths: list[str] = field(default_factory=list)
    task_tag: str | None = None

    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


class ActivityAggregator:
    """将单帧语义结果聚合为连续活动事件。"""

    def __init__(self, merge_window: timedelta = timedelta(minutes=3)) -> None:
        self.merge_window = merge_window
        self._pending: list[PageUnderstanding] = []
        self._events: list[ActivityEvent] = []
        self._seq = 0

    def ingest(self, frame: PageUnderstanding) -> list[ActivityEvent]:
        self._pending.append(frame)
        self._pending.sort(key=lambda f: f.analyzed_at)
        return self._flush_closed()

    def _flush_closed(self) -> list[ActivityEvent]:
        if len(self._pending) < 2:
            return []

        new_events: list[ActivityEvent] = []
        while len(self._pending) >= 2:
            first, second = self._pending[0], self._pending[1]
            gap = second.analyzed_at - first.analyzed_at
            same_activity = (
                first.page_category == second.page_category
                and first.user_action == second.user_action
                and gap <= self.merge_window
            )
            if not same_activity:
                evt = self._close_event([first])
                new_events.append(evt)
                self._events.append(evt)
                self._pending.pop(0)
            else:
                break

        if len(self._pending) == 1:
            last = self._pending[0]
            if datetime.now() - last.analyzed_at > self.merge_window:
                evt = self._close_event([last])
                new_events.append(evt)
                self._events.append(evt)
                self._pending.clear()

        return new_events

    def _close_event(self, frames: list[PageUnderstanding]) -> ActivityEvent:
        self._seq += 1
        start = min(f.analyzed_at for f in frames)
        end = max(f.analyzed_at for f in frames)
        primary = frames[-1]
        return ActivityEvent(
            event_id=f"act-{self._seq:05d}",
            started_at=start,
            ended_at=end,
            page_category=primary.page_category.value,
            user_action=primary.user_action.value,
            summary=primary.summary or f"{primary.page_category.value} / {primary.user_action.value}",
            evidence_paths=[f.screenshot_path for f in frames],
        )

    def flush_all(self) -> list[ActivityEvent]:
        if not self._pending:
            return []
        evt = self._close_event(self._pending)
        self._events.append(evt)
        self._pending.clear()
        return [evt]


class MemoryStore:
    """任务记忆 + 事件记忆 SQLite 存储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    page_category TEXT,
                    user_action TEXT,
                    summary TEXT,
                    evidence_paths TEXT,
                    task_tag TEXT
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_tag TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT DEFAULT 'open',
                    updated_at TEXT NOT NULL
                );
                """
            )

    def save_event(self, event: ActivityEvent) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events
                (event_id, started_at, ended_at, page_category, user_action, summary, evidence_paths, task_tag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.started_at.isoformat(),
                    event.ended_at.isoformat(),
                    event.page_category,
                    event.user_action,
                    event.summary,
                    "|".join(event.evidence_paths),
                    event.task_tag,
                ),
            )

    def list_events(self, limit: int = 50) -> list[ActivityEvent]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT event_id, started_at, ended_at, page_category, user_action, summary, evidence_paths, task_tag "
                "FROM events ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        result: list[ActivityEvent] = []
        for row in rows:
            paths = row[6].split("|") if row[6] else []
            result.append(
                ActivityEvent(
                    event_id=row[0],
                    started_at=datetime.fromisoformat(row[1]),
                    ended_at=datetime.fromisoformat(row[2]),
                    page_category=row[3],
                    user_action=row[4],
                    summary=row[5],
                    evidence_paths=paths,
                    task_tag=row[7],
                )
            )
        return result

    def time_by_category(self) -> dict[str, float]:
        events = self.list_events(limit=500)
        totals: dict[str, float] = {}
        for e in events:
            totals[e.page_category] = totals.get(e.page_category, 0.0) + e.duration_seconds()
        return totals
