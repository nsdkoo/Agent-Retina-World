"""MemoryStore v2：情节 + 语义 + 工作记忆三层 SQLite 存储。

在 v1 events/tasks 之上扩展：
- events 表加 importance 列（0-3，写入时启发式打分）
- facts 表：语义记忆（画像/偏好/项目/实体），带 confidence 与 last_seen_at，写入走对账（更新不追加）
- sessions + session_turns：工作记忆持久化，重启可恢复
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager


@contextmanager
def _db_connect(db_path: Path):
    """sqlite 上下文：自动 commit + close（Windows 下防止临时库文件锁）。"""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from screen_agent.activity.store import ActivityEvent

_FACT_CATEGORIES = ("profile", "preference", "project", "entity")

# 类型条件衰减半衰期（天）——ScrubJay-MEM 思路：不同记忆不同寿命
FACT_HALF_LIFE_DAYS = {
    "profile": 30.0,
    "preference": 14.0,
    "project": 7.0,
    "entity": 3.0,
}


def fact_decay(fact: Fact, now: datetime | None = None) -> float:
    """按类别半衰期算保留权重 0-1：profile 衰减慢、entity 衰减快。"""
    now = now or datetime.now()
    half_life = FACT_HALF_LIFE_DAYS.get(fact.category, 7.0) * 24.0
    age_hours = max((now - fact.last_seen_at).total_seconds(), 0.0) / 3600.0
    return 0.5 ** (age_hours / half_life)


def fact_score(fact: Fact, now: datetime | None = None) -> float:
    """检索排序分：confidence × 类型条件衰减。"""
    return fact.confidence * fact_decay(fact, now)


@dataclass
class Fact:
    fact_id: str
    category: str            # profile / preference / project / entity
    content: str
    source: str              # chat_rule / chat_llm / event_mining
    confidence: float        # 0-1
    created_at: datetime
    last_seen_at: datetime
    evidence: str = ""       # 溯源：来源轮次/事件 id（Agent Zero provenance 约束）


def score_event_importance(event: ActivityEvent) -> int:
    """情节记忆重要性启发式（0-3）：行为专注度 + 时长 + 场景类型。"""
    score = 1
    if event.user_action in ("debugging", "writing", "coding"):
        score += 1
    if event.duration_seconds() >= 600:
        score += 1
    if event.page_category in ("meeting", "documentation"):
        score = max(score, 2)
    return min(score, 3)


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。,.!！?？的得地]+", "", text.lower())


class MemoryStoreV2:
    """三层记忆统一存储（与 v1 events.db 同库，向后兼容）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with _db_connect(self.db_path) as conn:
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
                    frame_count INTEGER DEFAULT 1,
                    importance INTEGER DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_tag TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT DEFAULT 'open',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facts (
                    fact_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL DEFAULT 0.6,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    evidence TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );
                CREATE TABLE IF NOT EXISTS session_turns (
                    session_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY (session_id, idx)
                );
                CREATE TABLE IF NOT EXISTS intentions (
                    intention_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    due_at TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                """
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "frame_count" not in cols:
                conn.execute("ALTER TABLE events ADD COLUMN frame_count INTEGER DEFAULT 1")
            if "importance" not in cols:
                conn.execute("ALTER TABLE events ADD COLUMN importance INTEGER DEFAULT 1")
            fcols = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
            if "evidence" not in fcols:
                conn.execute("ALTER TABLE facts ADD COLUMN evidence TEXT DEFAULT ''")

    # ---- 情节记忆 ----

    def save_event(self, event: ActivityEvent) -> None:
        importance = score_event_importance(event)
        with _db_connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events
                (event_id, started_at, ended_at, page_category, user_action, summary,
                 evidence_paths, task_tag, frame_count, importance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    importance,
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

    def list_events(self, limit: int = 200) -> list[ActivityEvent]:
        with _db_connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT event_id, started_at, ended_at, page_category, user_action,
                       summary, evidence_paths, task_tag, frame_count
                FROM events ORDER BY started_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

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

    def bump_event_importance(self, event_id: str, delta: int = 1, cap: int = 3) -> int:
        """反思回填：事件被对话实际引用过 → importance 提权（封顶 cap）。返回新值。"""
        with _db_connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(importance, 1) FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return 0
            new_val = min(cap, int(row[0]) + delta)
            conn.execute(
                "UPDATE events SET importance = ? WHERE event_id = ?", (new_val, event_id)
            )
            return new_val

    def event_importance(self, event_id: str) -> int:
        with _db_connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT importance FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return int(row[0]) if row else 1

    @staticmethod
    def _row_to_event(row) -> ActivityEvent:
        return ActivityEvent(
            event_id=row[0],
            started_at=datetime.fromisoformat(row[1]),
            ended_at=datetime.fromisoformat(row[2]),
            page_category=row[3],
            user_action=row[4],
            summary=row[5],
            evidence_paths=row[6].split("|") if row[6] else [],
            task_tag=row[7],
            frame_count=row[8] or 1,
        )

    # ---- 语义记忆（写入走对账：更新不追加） ----

    def add_fact(
        self,
        category: str,
        content: str,
        source: str = "chat_rule",
        confidence: float = 0.6,
        evidence: str = "",
        supersede_keyword: str | None = None,
    ) -> Fact:
        """supersede_keyword：显式替换——同 category 下内容含该关键词的旧 fact
        降级为 confidence 0.15（保留供审计），避免陈旧偏好污染检索（Mem0 对账的显式形态）。"""
        """写 fact 前对账：同 category 下归一化内容相同 → 更新 last_seen 与 confidence，不追加。"""
        if category not in _FACT_CATEGORIES:
            raise ValueError(f"未知 fact 类别: {category}")
        norm = _normalize(content)
        now = datetime.now()
        with _db_connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT fact_id, content, confidence, evidence FROM facts WHERE category = ?",
                (category,),
            ).fetchall()
            for fact_id, existing, conf, old_ev in rows:
                if _normalize(existing) == norm:
                    new_conf = min(1.0, max(float(conf), confidence) + 0.1)
                    ev = evidence or (old_ev or "")
                    if supersede_keyword:
                        conn.execute(
                            "UPDATE facts SET confidence = 0.15 WHERE category = ? AND content LIKE ? AND fact_id != ?",
                            (category, f"%{supersede_keyword}%", fact_id),
                        )
                    conn.execute(
                        "UPDATE facts SET last_seen_at = ?, confidence = ?, evidence = ? WHERE fact_id = ?",
                        (now.isoformat(), new_conf, ev, fact_id),
                    )
                    return Fact(fact_id, category, existing, source, new_conf, now, now, ev)
            fact = Fact(
                fact_id=f"f-{uuid.uuid4().hex[:12]}",
                category=category,
                content=content,
                source=source,
                confidence=confidence,
                created_at=now,
                last_seen_at=now,
                evidence=evidence,
            )
            conn.execute(
                """
                INSERT INTO facts (fact_id, category, content, source, confidence, created_at, last_seen_at, evidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fact.fact_id, category, content, source, confidence, now.isoformat(), now.isoformat(), evidence),
            )
            if supersede_keyword:
                conn.execute(
                    "UPDATE facts SET confidence = 0.15 WHERE category = ? AND fact_id != ? AND content LIKE ?",
                    (category, fact.fact_id, f"%{supersede_keyword}%"),
                )
            return fact

    def list_facts(self, category: str | None = None, limit: int = 50) -> list[Fact]:
        sql = "SELECT fact_id, category, content, source, confidence, created_at, last_seen_at, evidence FROM facts"
        params: tuple = ()
        if category:
            sql += " WHERE category = ?"
            params = (category,)
        sql += " ORDER BY confidence DESC, last_seen_at DESC LIMIT ?"
        params += (limit,)
        with _db_connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Fact(r[0], r[1], r[2], r[3], float(r[4]), datetime.fromisoformat(r[5]), datetime.fromisoformat(r[6]), r[7] or "")
            for r in rows
        ]

    # ---- 工作记忆（会话持久化） ----

    def open_session(self) -> str:
        session_id = f"s-{uuid.uuid4().hex[:12]}"
        with _db_connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, started_at) VALUES (?, ?)",
                (session_id, datetime.now().isoformat()),
            )
        return session_id

    def close_session(self, session_id: str) -> None:
        with _db_connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
                (datetime.now().isoformat(), session_id),
            )

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        with _db_connect(self.db_path) as conn:
            idx = conn.execute(
                "SELECT COALESCE(MAX(idx), -1) + 1 FROM session_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO session_turns (session_id, idx, role, content) VALUES (?, ?, ?, ?)",
                (session_id, idx, role, content),
            )

    def latest_open_session(self) -> str | None:
        with _db_connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT s.session_id FROM sessions s
                WHERE s.ended_at IS NULL
                ORDER BY s.started_at DESC LIMIT 1
                """
            ).fetchone()
        return row[0] if row else None

    def load_session_turns(self, session_id: str, limit: int = 12) -> list[dict[str, str]]:
        """恢复工作记忆：取最近 limit 轮（系统内最多存 user/assistant 对）。"""
        with _db_connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT role, content FROM session_turns
                WHERE session_id = ? ORDER BY idx DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    # ---- 前瞻记忆（Typed Intention Store，2026-09 思路）----

    def add_intention(self, content: str, due_at: datetime | None = None) -> str:
        intention_id = f"i-{uuid.uuid4().hex[:12]}"
        with _db_connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO intentions (intention_id, content, due_at, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                (intention_id, content, due_at.isoformat() if due_at else None, datetime.now().isoformat()),
            )
        return intention_id

    def list_intentions(self, status: str = "pending", limit: int = 20) -> list[dict]:
        with _db_connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT intention_id, content, due_at, status, created_at FROM intentions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [
            {
                "intention_id": r[0],
                "content": r[1],
                "due_at": r[2],
                "status": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def due_intentions(self, now: datetime | None = None) -> list[dict]:
        """到点的前瞻记忆：due_at 非空且已过期（now 之后不再含未来意图）。"""
        now = now or datetime.now()
        return [
            it
            for it in self.list_intentions(status="pending", limit=50)
            if it["due_at"] and datetime.fromisoformat(it["due_at"]) <= now
        ]

    def complete_intention(self, intention_id: str) -> None:
        with _db_connect(self.db_path) as conn:
            conn.execute(
                "UPDATE intentions SET status = 'done', completed_at = ? WHERE intention_id = ?",
                (datetime.now().isoformat(), intention_id),
            )
