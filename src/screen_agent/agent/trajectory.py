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
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from screen_agent.agent.events import Event
from screen_agent.agent.state import AgentState, TaskState

logger = logging.getLogger(__name__)

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
    started_at  TEXT,
    ended_at    TEXT,
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
CREATE TABLE IF NOT EXISTS spans (
    span_id        TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    parent_span_id TEXT,
    name           TEXT,
    kind           TEXT,
    start_ts       REAL,
    end_ts         REAL,
    status         TEXT,
    attributes     TEXT,
    events         TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
"""

_UNFINISHED = (
    TaskState.PENDING.value,
    TaskState.PLANNING.value,
    TaskState.RUNNING.value,
    TaskState.AWAITING_USER_CONFIRMATION.value,
    TaskState.AWAITING_USER_INPUT.value,
    TaskState.ERROR.value,
)

# 给既有库补列。原来 steps 表只存 `elapsed_ms` 不存起止时间，
# 于是读回来的任务耗时信息是死的（`elapsed_ms` 算不出来）、也无法用 from_dict 还原。
# 加列是幂等的：已经有的列会报 duplicate column，忽略即可，新老库共用同一份代码。
_MIGRATIONS = (
    "ALTER TABLE steps ADD COLUMN started_at TEXT",
    "ALTER TABLE steps ADD COLUMN ended_at TEXT",
)


def _safe_json(raw: str | None) -> dict:
    """解析 JSON 对象。一条脏数据不该让整个任务恢复不了 / 回放不了，所以失败返回空 dict。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _column(row: sqlite3.Row, name: str) -> str | None:
    """读可能不存在的列——老库在迁移之前没有 started_at / ended_at。"""
    return row[name] if name in row.keys() else None


def _span_row(row: sqlite3.Row) -> dict:
    """把 spans 表的一行还原成 dict。events 是数组、attributes 是对象，分开解析。"""
    raw_events = row["events"]
    try:
        events = json.loads(raw_events) if raw_events else []
    except (TypeError, ValueError):
        events = []
    return {
        "span_id": row["span_id"],
        "trace_id": row["trace_id"],
        "parent_span_id": row["parent_span_id"],
        "name": row["name"],
        "kind": row["kind"],
        "start_ts": row["start_ts"],
        "end_ts": row["end_ts"],
        "status": row["status"],
        "attributes": _safe_json(row["attributes"]),
        "events": events if isinstance(events, list) else [],
    }


class TrajectoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            for statement in _MIGRATIONS:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    pass    # 列已存在，跳过

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
                                      observation, success, elapsed_ms, started_at, ended_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        state.task_id, s.index, s.goal, s.tool,
                        json.dumps(s.params, ensure_ascii=False), s.why,
                        s.status.value, s.observation, int(s.success), s.elapsed_ms,
                        s.started_at.isoformat() if s.started_at else None,
                        s.ended_at.isoformat() if s.ended_at else None,
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

        # 统一交给 `AgentState.from_dict`，不再手写反序列化——
        # 原先那版手写的**丢了 started_at/ended_at 和 plan**，
        # 导致读回来的任务耗时永远是 0。两处各写各的，迟早漂移。
        payload = {
            "task_id": row["task_id"],
            "goal": row["goal"],
            "state": row["state"],
            "cursor": row["cursor"],
            "iteration": row["iteration"],
            "result": row["result"] or "",
            "error": row["error"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "steps": [
                {
                    "index": r["idx"],
                    "goal": r["goal"] or "",
                    "tool": r["tool"] or "",
                    "params": _safe_json(r["params"]),
                    "why": r["why"] or "",
                    "status": r["status"],
                    "observation": r["observation"] or "",
                    "success": bool(r["success"]),
                    "started_at": _column(r, "started_at"),
                    "ended_at": _column(r, "ended_at"),
                }
                for r in step_rows
            ],
        }
        return AgentState.from_dict(payload)

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

    # ---- trace / 回放 ----
    # events 表以前**只写不读**，相当于花了写盘的钱却没换来任何排障能力；
    # spans 表是这次新加的，两者合起来才支撑得起「回放一次执行」。

    def save_spans(self, spans: list) -> None:
        """落 span。失败只记 debug——**可观测性不能反过来拖垮主流程**。"""
        if not spans:
            return
        try:
            with self._connect() as conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO spans
                       (span_id, trace_id, parent_span_id, name, kind,
                        start_ts, end_ts, status, attributes, events)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            s.span_id, s.trace_id, s.parent_span_id, s.name, s.kind,
                            s.start_ts, s.end_ts, s.status,
                            json.dumps(s.attributes, ensure_ascii=False),
                            json.dumps(s.events, ensure_ascii=False),
                        )
                        for s in spans
                    ],
                )
        except (sqlite3.Error, TypeError, AttributeError):
            logger.debug("span 落盘失败", exc_info=True)

    def read_spans(self, task_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM spans WHERE trace_id=? ORDER BY start_ts", (task_id,)
            ).fetchall()
        return [_span_row(r) for r in rows]

    def read_events(self, task_id: str, kind: str | None = None,
                    limit: int | None = None) -> list[dict]:
        """读事件。补上「events 表只写不读」这个缺口。"""
        sql = "SELECT * FROM events WHERE task_id=?"
        args: list = [task_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [
            {
                "kind": r["kind"], "step": r["step"], "ts": r["ts"],
                "payload": _safe_json(r["payload"]),
            }
            for r in rows
        ]

    def replay(self, task_id: str) -> list[dict]:
        """把一次任务还原成一条按时间排序的时间线，供人眼看。

        合并两个来源：spans（结构化执行）与 events（事件流快照）。
        从上到下读一遍就能回答「这任务到底经历了什么」。
        """
        timeline: list[dict] = []
        for span in self.read_spans(task_id):
            start = span.get("start_ts") or 0.0
            end = span.get("end_ts") or start
            timeline.append({
                "at": start,
                "type": "span",
                "name": span.get("name"),
                "kind": span.get("kind"),
                "status": span.get("status"),
                "duration_ms": int((end - start) * 1000),
                "attributes": span.get("attributes") or {},
                "events": span.get("events") or [],
            })
        for event in self.read_events(task_id):
            timeline.append({
                "at": event.get("ts") or 0.0,
                "type": "event",
                "name": event.get("kind"),
                "kind": "event",
                "step": event.get("step", 0),
                "attributes": event.get("payload") or {},
                "events": [],
            })
        timeline.sort(key=lambda item: item["at"])
        return timeline

    def export_otel(self, task_id: str) -> list[dict]:
        """导出成贴近 OTLP 的形状。

        字段名按 OTel 的来（traceId / spanId / startTimeUnixNano…），
        将来真要接 Collector，补一个 exporter 就够，数据层不用再动。
        """
        exported = []
        for span in self.read_spans(task_id):
            start = span.get("start_ts") or 0.0
            end = span.get("end_ts") or start
            exported.append({
                "traceId": span.get("trace_id"),
                "spanId": span.get("span_id"),
                "parentSpanId": span.get("parent_span_id") or "",
                "name": span.get("name"),
                "kind": span.get("kind"),
                "startTimeUnixNano": int(start * 1_000_000_000),
                "endTimeUnixNano": int(end * 1_000_000_000),
                "status": {"code": span.get("status")},
                "attributes": span.get("attributes") or {},
                "events": span.get("events") or [],
            })
        return exported
