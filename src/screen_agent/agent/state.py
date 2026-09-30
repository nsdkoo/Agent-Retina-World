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
        TaskState.FINISHED, TaskState.REJECTED, TaskState.ERROR, TaskState.STOPPED,
    },
    TaskState.AWAITING_USER_CONFIRMATION: {TaskState.RUNNING, TaskState.REJECTED, TaskState.STOPPED},
    TaskState.AWAITING_USER_INPUT: {TaskState.RUNNING, TaskState.STOPPED},
    TaskState.FINISHED: set(),
    TaskState.REJECTED: set(),
    TaskState.ERROR: {TaskState.PLANNING, TaskState.RUNNING, TaskState.STOPPED},
    TaskState.STOPPED: {TaskState.PLANNING, TaskState.RUNNING},
}


class IllegalTransition(RuntimeError):
    """非法状态跳转。"""


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
        return f"{self.state.value} · {done}/{total} 步"
