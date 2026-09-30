"""任务轨迹持久化：任务、步骤、事件落 SQLite，支持跨重启续跑。

对标 Cline 的任务存储（每个任务独立存盘、每步写一次）与 goose 的 session 持久化。
三张表：

- `tasks`  任务级状态（目标、状态、游标、结果、时间）
- `steps`  每步的 Action 与 Observation（含耗时）
- `events` 事件流快照（plan / action / observation / ask / finish），供回放排障

用途：断电或重启后能回答「昨天那个整理任务做到哪了」，也能直接续跑没做完的。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from screen_agent.agent.events import Event
from screen_agent.agent.state import AgentState, StepRecord, StepStatus, TaskState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id    TEXT PRIMARY KEY,
    goal       TEXT NOT NULL,
    state      TEXT NOT NULL,
    cursor     INTEGER DEFAULT 0,
    iteration  INTEGER DEFAULT 0,
    result     TEXT DEFAULT '',
    error      TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    task_id     TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    goal        TEXT,
    tool        TEXT,
    params      TEXT,
    why         TEXT,
    status      TEXT,
    observation TEXT,
    success     INTEGER DEFAULT 0,
    elapsed_ms  INTEGER DEFAULT 0,
    PRIMARY KEY (task_id, idx)
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  TEXT,
    kind     TEXT,
    step     INTEGER DEFAULT 0,
    payload  TEXT,
    ts       REAL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
"""

_UNFINISHED = (
    TaskState.PENDING.value,
    TaskState.PLANNING.value,
    TaskState.RUNNING.value,
    TaskState.AWAITING_USER_CONFIRMATION.value,
    TaskState.AWAITING_USER_INPUT.value,
    TaskState.ERROR.value,
)


class TrajectoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        """sqlite 连接上下文：必须显式 close。

        Windows 上 `with sqlite3.connect(...)` 只是事务上下文（commit / rollback），
        连接根本不关；临时库文件于是被锁住，测试清理时报 WinError 32。
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- 写 ----

    def save(self, state: AgentState) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tasks (task_id, goal, state, cursor, iteration, result, error,
                                      created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       state=excluded.state, cursor=excluded.cursor,
                       iteration=excluded.iteration, result=excluded.result,
                       error=excluded.error, updated_at=excluded.updated_at""",
                (
                    state.task_id, state.goal, state.state.value, state.cursor,
                    state.iteration, state.result, state.error,
                    state.created_at.isoformat(), state.updated_at.isoformat(),
                ),
            )
            conn.execute("DELETE FROM steps WHERE task_id=?", (state.task_id,))
            conn.executemany(
                """INSERT INTO steps (task_id, idx, goal, tool, params, why, status,
                                      observation, success, elapsed_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        state.task_id, s.index, s.goal, s.tool,
                        json.dumps(s.params, ensure_ascii=False), s.why,
                        s.status.value, s.observation, int(s.success), s.elapsed_ms,
                    )
                    for s in state.steps
                ],
            )

    def log_event(self, event: Event) -> None:
        if event.type.value in ("user_message", "state_changed"):
            return  # 只留能支撑回放的骨架事件，别让表无限膨胀
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO events (task_id, kind, step, payload, ts) VALUES (?,?,?,?,?)",
                    (
                        event.task_id, event.type.value, event.step,
                        json.dumps(event.payload, ensure_ascii=False)[:2000], event.ts,
                    ),
                )
        except (sqlite3.Error, TypeError):
            pass

    # ---- 读 ----

    def load(self, task_id: str) -> AgentState | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return None
            step_rows = conn.execute(
                "SELECT * FROM steps WHERE task_id=? ORDER BY idx", (task_id,)
            ).fetchall()
        state = AgentState(
            goal=row["goal"],
            task_id=row["task_id"],
            state=TaskState(row["state"]),
            cursor=row["cursor"],
            iteration=row["iteration"],
            result=row["result"] or "",
            error=row["error"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        state.steps = [
            StepRecord(
                index=r["idx"], goal=r["goal"] or "", tool=r["tool"] or "",
                params=json.loads(r["params"] or "{}"), why=r["why"] or "",
                status=StepStatus(r["status"]), observation=r["observation"] or "",
                success=bool(r["success"]),
            )
            for r in step_rows
        ]
        return state

    def latest_unfinished(self) -> AgentState | None:
        """最近一条没跑完的任务，供启动时提示「要不要接着做」。"""
        marks = ",".join("?" * len(_UNFINISHED))
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT task_id FROM tasks WHERE state IN ({marks}) "
                f"ORDER BY updated_at DESC LIMIT 1",
                _UNFINISHED,
            ).fetchone()
        return self.load(row["task_id"]) if row else None

    def recent(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, goal, state, result, updated_at FROM tasks "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
