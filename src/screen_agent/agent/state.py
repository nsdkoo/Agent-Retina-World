"""任务状态机与步骤记录。

对标 OpenHands 的 AgentState：不只记「现在到第几步」，还要留每一步的 Action/Observation、
长期计划、以及断点恢复所需的游标。状态迁移表显式声明，非法跳转直接拒绝——
宁可早报错，也不要让任务卡在一个说不清的状态里。

    PENDING → PLANNING → RUNNING ⇄ AWAITING_USER_CONFIRMATION / AWAITING_USER_INPUT
                                  ↘ FINISHED / REJECTED / ERROR / STOPPED
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TaskState(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_USER_CONFIRMATION = "awaiting_confirmation"
    AWAITING_USER_INPUT = "awaiting_input"
    FINISHED = "finished"
    REJECTED = "rejected"
    # 和 REJECTED / STOPPED 区分开：
    #   REJECTED  = 在门口就被拒（挂起时用户说不做）
    #   STOPPED   = 系统主动停机（迭代上限等）
    #   CANCELLED = 任务已经跑起来了，用户中途把它叫停
    CANCELLED = "cancelled"
    ERROR = "error"
    STOPPED = "stopped"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    AWAITING = "awaiting"


_ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.PLANNING, TaskState.RUNNING, TaskState.ERROR, TaskState.STOPPED},
    TaskState.PLANNING: {
        TaskState.RUNNING, TaskState.AWAITING_USER_CONFIRMATION,
        TaskState.FINISHED, TaskState.ERROR, TaskState.STOPPED,
    },
    TaskState.RUNNING: {
        TaskState.PLANNING, TaskState.AWAITING_USER_CONFIRMATION, TaskState.AWAITING_USER_INPUT,
        TaskState.FINISHED, TaskState.REJECTED, TaskState.CANCELLED,
        TaskState.ERROR, TaskState.STOPPED,
    },
    TaskState.AWAITING_USER_CONFIRMATION: {
        TaskState.RUNNING, TaskState.REJECTED, TaskState.CANCELLED, TaskState.STOPPED,
    },
    TaskState.AWAITING_USER_INPUT: {
        TaskState.RUNNING, TaskState.CANCELLED, TaskState.STOPPED,
    },
    TaskState.FINISHED: set(),
    TaskState.REJECTED: set(),
    TaskState.CANCELLED: set(),      # 终态：用户中途叫停，不再往下走
    TaskState.ERROR: {TaskState.PLANNING, TaskState.RUNNING, TaskState.STOPPED},
    TaskState.STOPPED: {TaskState.PLANNING, TaskState.RUNNING},
}


class IllegalTransition(RuntimeError):
    """非法状态跳转。"""


def _parse_dt(value: str | None) -> datetime | None:
    """容错解析时间戳。旧记录可能是空、也可能格式不同——**解析不了就当没有**，
    不能因为一个时间戳让整条历史任务读不回来。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _to_task_state(value: str | None) -> TaskState:
    try:
        return TaskState(value)
    except (TypeError, ValueError):
        return TaskState.PENDING


def _to_step_status(value: str | None) -> StepStatus:
    try:
        return StepStatus(value)
    except (TypeError, ValueError):
        return StepStatus.PENDING


@dataclass
class PlanStep:
    """一步要做的事；why 是给人看的理由，直接进 UI。"""

    goal: str
    tool: str = ""
    params: dict = field(default_factory=dict)
    why: str = ""


@dataclass
class StepRecord:
    index: int
    goal: str
    tool: str
    params: dict
    why: str = ""
    status: StepStatus = StepStatus.PENDING
    observation: str = ""
    success: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def elapsed_ms(self) -> int:
        if self.started_at is None or self.ended_at is None:
            return 0
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


@dataclass
class AgentState:
    """一次任务的完整状态；可序列化，落库后可续跑。"""

    goal: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: TaskState = TaskState.PENDING
    plan: list[PlanStep] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    cursor: int = 0
    iteration: int = 0
    result: str = ""
    error: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    # ---- 状态迁移 ----

    def can_transition(self, target: TaskState) -> bool:
        return target in _ALLOWED.get(self.state, set())

    def transition(self, target: TaskState) -> tuple[TaskState, TaskState]:
        if not self.can_transition(target):
            raise IllegalTransition(f"{self.state.value} → {target.value} 不允许")
        previous, self.state = self.state, target
        self.updated_at = datetime.now()
        return previous, target

    def finish(self, result: str) -> None:
        self.result = result
        self.state = TaskState.FINISHED
        self.updated_at = datetime.now()

    def fail(self, error: str) -> None:
        self.error = error
        self.state = TaskState.ERROR
        self.updated_at = datetime.now()

    # ---- 步骤 ----

    def load_plan(self, plan: list[PlanStep]) -> None:
        self.plan = list(plan)
        self.steps = [
            StepRecord(index=i, goal=s.goal, tool=s.tool, params=dict(s.params), why=s.why)
            for i, s in enumerate(plan)
        ]
        self.cursor = 0
        self.updated_at = datetime.now()

    def current_step(self) -> StepRecord | None:
        if 0 <= self.cursor < len(self.steps):
            return self.steps[self.cursor]
        return None

    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.DONE)

    def failed_steps(self) -> list[StepRecord]:
        return [s for s in self.steps if s.status == StepStatus.FAILED]

    # ---- 序列化（落库 / 恢复）----

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "state": self.state.value,
            "cursor": self.cursor,
            "iteration": self.iteration,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            # plan 也要序列化：以前漏了它，`from_dict(to_dict())` 就不是无损的，
            # 续跑后拿不到原始计划（steps 虽然能反推，但那是变通不是还原）
            "plan": [
                {"goal": p.goal, "tool": p.tool, "params": p.params, "why": p.why}
                for p in self.plan
            ],
            "steps": [
                {
                    "index": s.index, "goal": s.goal, "tool": s.tool, "params": s.params,
                    "why": s.why, "status": s.status.value, "observation": s.observation,
                    "success": s.success,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                }
                for s in self.steps
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentState:
        """`to_dict()` 的逆运算，用于从库里把任务读回来续跑。

        所有字段都做了缺失兜底——**旧记录没有 plan、没有时间戳**，
        读的时候不能因为缺键就炸掉，那样等于让历史任务永久不可恢复。
        """
        steps = [
            StepRecord(
                index=int(row.get("index", i)),
                goal=row.get("goal", ""),
                tool=row.get("tool", ""),
                params=dict(row.get("params") or {}),
                why=row.get("why", ""),
                status=_to_step_status(row.get("status")),
                observation=row.get("observation", ""),
                success=bool(row.get("success")),
                started_at=_parse_dt(row.get("started_at")),
                ended_at=_parse_dt(row.get("ended_at")),
            )
            for i, row in enumerate(data.get("steps") or [])
        ]
        plan_rows = data.get("plan")
        if plan_rows:
            plan = [
                PlanStep(
                    goal=p.get("goal", ""), tool=p.get("tool", ""),
                    params=dict(p.get("params") or {}), why=p.get("why", ""),
                )
                for p in plan_rows
            ]
        else:
            # 旧记录没有 plan：由 steps 反推，保证续跑时 plan 不空
            plan = [
                PlanStep(goal=s.goal, tool=s.tool, params=dict(s.params), why=s.why)
                for s in steps
            ]
        return cls(
            goal=data.get("goal", ""),
            task_id=data.get("task_id") or uuid.uuid4().hex[:12],
            state=_to_task_state(data.get("state")),
            plan=plan,
            steps=steps,
            cursor=int(data.get("cursor", 0)),
            iteration=int(data.get("iteration", 0)),
            result=data.get("result", ""),
            error=data.get("error", ""),
            created_at=_parse_dt(data.get("created_at")) or datetime.now(),
            updated_at=_parse_dt(data.get("updated_at")) or datetime.now(),
        )

    # ---- 汇报 ----

    def summary(self) -> str:
        done, total = self.done_count(), len(self.steps)
        if self.state == TaskState.FINISHED:
            head = self.result or f"{done}/{total} 步完成"
            failed = self.failed_steps()
            if failed:
                head += f"（{len(failed)} 步没成）"
            return head
        if self.state == TaskState.ERROR:
            return f"任务中断：{self.error or '未知错误'}（已完成 {done}/{total} 步）"
        if self.state == TaskState.REJECTED:
            return f"好，已取消（当时走到 {done}/{total} 步）"
        if self.state == TaskState.CANCELLED:
            return f"好，停下了（已做完 {done}/{total} 步）"
        return f"{self.state.value} · {done}/{total} 步"
