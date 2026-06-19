from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from screen_agent.understand.vlm import PageUnderstanding


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
    frame_count: int = 1

    def duration_seconds(self) -> float:
        return max((self.ended_at - self.started_at).total_seconds(), 1.0)


class ActivityAggregator:
    """将单帧语义结果聚合为连续活动事件。"""

    def __init__(self, merge_window: timedelta = timedelta(minutes=3)) -> None:
        self.merge_window = merge_window
        self._run: list[PageUnderstanding] = []
        self._seq = 0

    def _same_activity(self, a: PageUnderstanding, b: PageUnderstanding) -> bool:
        gap = b.analyzed_at - a.analyzed_at
        return (
            a.page_category == b.page_category
            and a.user_action == b.user_action
            and gap <= self.merge_window
        )

    def ingest(self, frame: PageUnderstanding) -> list[ActivityEvent]:
        if not self._run:
            self._run = [frame]
            return []

        last = self._run[-1]
        if self._same_activity(last, frame):
            self._run.append(frame)
            return []

        event = self._close_run(self._run)
        self._run = [frame]
        return [event]

    def _close_run(self, frames: list[PageUnderstanding]) -> ActivityEvent:
        self._seq += 1
        start = min(f.analyzed_at for f in frames)
        end = max(f.analyzed_at for f in frames)
        primary = frames[-1]
        tag = f"{primary.page_category.value}:{primary.user_action.value}"
        return ActivityEvent(
            event_id=f"act-{self._seq:05d}",
            started_at=start,
            ended_at=end,
            page_category=primary.page_category.value,
            user_action=primary.user_action.value,
            summary=primary.summary or f"{primary.page_category.value} / {primary.user_action.value}",
            evidence_paths=[f.screenshot_path for f in frames],
            task_tag=tag,
            frame_count=len(frames),
        )

    def flush_all(self) -> list[ActivityEvent]:
        if not self._run:
            return []
        event = self._close_run(self._run)
        self._run = []
        return [event]


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
                    task_tag TEXT,
                    frame_count INTEGER DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_tag TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT DEFAULT 'open',
                    updated_at TEXT NOT NULL
                );
                """
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "frame_count" not in cols:
                conn.execute("ALTER TABLE events ADD COLUMN frame_count INTEGER DEFAULT 1")

    def save_event(self, event: ActivityEvent) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events
                (event_id, started_at, ended_at, page_category, user_action, summary,
                 evidence_paths, task_tag, frame_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    event.frame_count,
                ),
            )
            if event.task_tag:
                conn.execute(
                    """
                    INSERT INTO tasks (task_tag, title, status, updated_at)
                    VALUES (?, ?, 'open', ?)
                    ON CONFLICT(task_tag) DO UPDATE SET updated_at=excluded.updated_at
                    """,
                    (event.task_tag, event.summary[:120], datetime.now().isoformat()),
                )

    def list_events(self, limit: int = 50) -> list[ActivityEvent]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT event_id, started_at, ended_at, page_category, user_action,
                       summary, evidence_paths, task_tag, frame_count
                FROM events ORDER BY started_at DESC LIMIT ?
                """,
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
                    frame_count=row[8] or 1,
                )
            )
        return result

    def time_by_category(self) -> dict[str, float]:
        events = self.list_events(limit=500)
        totals: dict[str, float] = {}
        for e in events:
            totals[e.page_category] = totals.get(e.page_category, 0.0) + e.duration_seconds()
        return totals

    def summary_stats(self) -> dict:
        events = self.list_events(limit=500)
        if not events:
            return {"event_count": 0, "total_frames": 0, "categories": {}}
        return {
            "event_count": len(events),
            "total_frames": sum(e.frame_count for e in events),
            "categories": self.time_by_category(),
        }
