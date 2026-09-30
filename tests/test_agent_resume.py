"""跨进程续跑的测试。

改这个的起因：`latest_unfinished()` 定义了却**没有任何调用方**，
`trajectory.load()` 也是手写的反序列化（还丢了 `started_at/ended_at`）。
结果是——重启后正在跑的任务凭空消失，昨天那个整理任务做到哪了也答不上来。

业界把「状态外部化 + 可寻址恢复」列为硬标准（LangGraph 的 checkpointer、
Anthropic 的 Session、Google ADK 的 SessionService 都是这个思路），
所以这组测试要守住三件事：

1. **序列化是无损的**：`from_dict(to_dict())` 逐字段相等，包括时间戳与 plan
2. **能真的续跑**：落库后的任务，换一个 controller 实例能接着跑完
3. **残步不乱来**：崩溃时正在执行的步，幂等的重跑、非幂等的**绝不重跑**
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent.controller import AgentController
from screen_agent.agent.policy import PolicyEngine, PermissionMode
from screen_agent.agent.state import (
    AgentState,
    PlanStep,
    StepRecord,
    StepStatus,
    TaskState,
)
from screen_agent.agent.trajectory import TrajectoryStore
from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec


class _FakePlanner:
    """固定计划，不依赖 LLM。"""

    def __init__(self, steps: list[PlanStep]) -> None:
        self._steps = steps

    def plan(self, goal: str) -> list[PlanStep]:  # noqa: ARG002
        return list(self._steps)

    def looks_like_task(self, text: str) -> bool:  # noqa: ARG002
        return True


def _echo(name: str, idempotent: bool = False, log: list[str] | None = None) -> ToolSpec:
    def _handler(**params) -> ActionResult:
        if log is not None:
            log.append(name)
        return ActionResult(success=True, message=f"{name} ok {params}")

    return ToolSpec(name, f"演示工具 {name}", _handler, RiskLevel.SAFE, idempotent=idempotent)


class StateRoundTripTests(unittest.TestCase):
    def _sample(self) -> AgentState:
        started = datetime(2026, 9, 30, 10, 0, 0)
        state = AgentState(goal="把桌面文件归档", task_id="t-roundtrip")
        state.load_plan([
            PlanStep(goal="列目录", tool="files.list", params={"path": "桌面"}, why="先看看"),
            PlanStep(goal="按类型归类", tool="files.organize", params={"mode": "type"}, why="归档"),
        ])
        state.steps[0].status = StepStatus.DONE
        state.steps[0].success = True
        state.steps[0].observation = "找到 12 个文件"
        state.steps[0].started_at = started
        state.steps[0].ended_at = started + timedelta(milliseconds=250)
        state.cursor = 1
        state.iteration = 3
        state.state = TaskState.RUNNING
        return state

    def test_roundtrip_is_lossless(self) -> None:
        original = self._sample()
        restored = AgentState.from_dict(original.to_dict())

        self.assertEqual(restored.goal, original.goal)
        self.assertEqual(restored.task_id, original.task_id)
        self.assertEqual(restored.state, original.state)
        self.assertEqual(restored.cursor, original.cursor)
        self.assertEqual(restored.iteration, original.iteration)
        self.assertEqual(restored.created_at, original.created_at)
        self.assertEqual(restored.updated_at, original.updated_at)

    def test_plan_survives_roundtrip(self) -> None:
        """`plan` 以前根本没进 to_dict，续跑会拿不到原始计划。"""
        original = self._sample()
        restored = AgentState.from_dict(original.to_dict())
        self.assertEqual(len(restored.plan), 2)
        self.assertEqual(restored.plan[1].tool, "files.organize")

    def test_step_timestamps_survive_roundtrip(self) -> None:
        """耗时信息以前在往返里丢掉，`elapsed_ms` 永远算成 0。"""
        original = self._sample()
        restored = AgentState.from_dict(original.to_dict())
        self.assertEqual(restored.steps[0].started_at, original.steps[0].started_at)
        self.assertEqual(restored.steps[0].ended_at, original.steps[0].ended_at)
        self.assertEqual(restored.steps[0].elapsed_ms, 250)

    def test_from_dict_tolerates_legacy_rows(self) -> None:
        """旧记录没有 plan、没有时间戳——不能因为缺键就让历史任务永久不可恢复。"""
        legacy = {
            "task_id": "t-legacy",
            "goal": "老任务",
            "state": "running",
            "cursor": 1,
            "steps": [
                {"index": 0, "goal": "第一步", "tool": "files.list", "status": "done"},
                {"index": 1, "goal": "第二步", "tool": "files.glob", "status": "pending"},
            ],
        }
        state = AgentState.from_dict(legacy)
        self.assertEqual(state.task_id, "t-legacy")
        self.assertEqual(len(state.steps), 2)
        self.assertEqual(len(state.plan), 2, "旧记录应由 steps 反推出 plan")
        self.assertIsNotNone(state.created_at)

    def test_from_dict_survives_garbage_values(self) -> None:
        state = AgentState.from_dict({"goal": "x", "state": "不存在的状态",
                                      "created_at": "不是时间戳"})
        self.assertEqual(state.state, TaskState.PENDING)
        self.assertIsInstance(state.created_at, datetime)


class TrajectoryLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def test_load_restores_elapsed_ms(self) -> None:
        """落库再读回来，耗时信息不能丢（这是原先手写反序列化的 bug）。"""
        state = AgentState(goal="计时任务", task_id="t-timing")
        state.load_plan([PlanStep(goal="干活", tool="files.list")])
        started = datetime.now()
        state.steps[0].status = StepStatus.DONE
        state.steps[0].success = True
        state.steps[0].started_at = started
        state.steps[0].ended_at = started + timedelta(milliseconds=400)

        self.store.save(state)
        loaded = self.store.load("t-timing")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.steps[0].elapsed_ms, 400)

    def test_load_returns_none_for_missing(self) -> None:
        self.assertIsNone(self.store.load("no-such-task"))

    def test_latest_unfinished_prefers_recent(self) -> None:
        old = AgentState(goal="旧任务", task_id="t-old")
        old.state = TaskState.RUNNING
        self.store.save(old)
        new = AgentState(goal="新任务", task_id="t-new")
        new.state = TaskState.RUNNING
        self.store.save(new)

        latest = self.store.latest_unfinished()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.task_id, "t-new")

    def test_finished_task_is_not_unfinished(self) -> None:
        done = AgentState(goal="做完的", task_id="t-done")
        done.state = TaskState.FINISHED
        self.store.save(done)
        self.assertIsNone(self.store.latest_unfinished())

    def test_migration_adds_columns_to_old_db(self) -> None:
        """老库没有 started_at/ended_at 列，重开时要能自动补上而不是报错。"""
        import sqlite3

        path = Path(self._tmp.name) / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE steps (task_id TEXT, idx INTEGER, goal TEXT, tool TEXT, "
            "params TEXT, why TEXT, status TEXT, observation TEXT, success INTEGER, "
            "elapsed_ms INTEGER, PRIMARY KEY (task_id, idx))"
        )
        conn.commit()
        conn.close()

        store = TrajectoryStore(path)          # 触发迁移
        state = AgentState(goal="迁移后能用", task_id="t-mig")
        state.load_plan([PlanStep(goal="干活", tool="files.list")])
        state.steps[0].started_at = datetime.now()
        state.steps[0].ended_at = datetime.now()
        store.save(state)
        self.assertEqual(store.load("t-mig").task_id, "t-mig")


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")
        self.log: list[str] = []
        self.registry = ToolRegistry()
        self.registry.register(_echo("files.list", idempotent=True, log=self.log))
        self.registry.register(_echo("files.glob", idempotent=True, log=self.log))
        self.registry.register(_echo("files.move", idempotent=False, log=self.log))

    def _controller(self, steps: list[PlanStep]) -> AgentController:
        return AgentController(
            registry=self.registry,
            planner=_FakePlanner(steps),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.store,
        )

    def test_resume_continues_remaining_steps(self) -> None:
        """落库后的任务，换一个 controller 实例要能接着跑完。"""
        steps = [
            PlanStep(goal="第一步", tool="files.list"),
            PlanStep(goal="第二步", tool="files.glob"),
        ]
        first = self._controller(steps)
        first.run("归档任务")
        # 人为把它打回未完成状态，模拟"跑到一半进程没了"
        state = self.store.load(first.state.task_id)
        state.state = TaskState.RUNNING
        state.cursor = 1
        state.steps[1].status = StepStatus.PENDING
        self.store.save(state)

        second = self._controller(steps)
        result = second.resume_from(first.state.task_id)
        self.assertIsNotNone(result)
        self.assertIn("files.glob", self.log)

    def test_resume_returns_none_for_finished(self) -> None:
        steps = [PlanStep(goal="干活", tool="files.list")]
        controller = self._controller(steps)
        controller.run("一次就做完")
        self.assertIsNone(controller.resume_from(controller.state.task_id))

    def test_resume_returns_none_without_trajectory(self) -> None:
        controller = AgentController(
            registry=self.registry, planner=_FakePlanner([]),
            policy=PolicyEngine(mode=PermissionMode.AUTO), trajectory=None,
        )
        self.assertIsNone(controller.resume_from("whatever"))

    def test_resume_requeues_running_idempotent_step(self) -> None:
        """崩溃时正在跑的**幂等**步 → 重置成 PENDING 重跑。"""
        steps = [PlanStep(goal="读目录", tool="files.list")]
        controller = self._controller(steps)
        state = AgentState(goal="读目录", task_id="t-resume-idem")
        state.load_plan(steps)
        state.state = TaskState.RUNNING
        state.steps[0].status = StepStatus.RUNNING   # 崩在这一步
        self.store.save(state)

        self.log.clear()
        controller.resume_from("t-resume-idem")
        self.assertIn("files.list", self.log, "幂等残步应当被重跑")

    def test_resume_marks_running_nonidempotent_failed(self) -> None:
        """崩溃时正在跑的**非幂等**步 → 如实记失败并跳过，绝不重跑。"""
        steps = [
            PlanStep(goal="移文件", tool="files.move", params={"src": "a", "dst": "b"}),
            PlanStep(goal="再读一次", tool="files.list"),
        ]
        controller = self._controller(steps)
        state = AgentState(goal="移文件", task_id="t-resume-write")
        state.load_plan(steps)
        state.state = TaskState.RUNNING
        state.steps[0].status = StepStatus.RUNNING   # 崩在这一步
        self.store.save(state)

        self.log.clear()
        controller.resume_from("t-resume-write")
        self.assertNotIn("files.move", self.log, "非幂等残步被重跑了——可能产生二次副作用")
        reloaded = self.store.load("t-resume-write")
        self.assertEqual(reloaded.steps[0].status, StepStatus.FAILED)
        self.assertIn("结果未知", reloaded.steps[0].observation)

    def test_resume_reasks_when_awaiting(self) -> None:
        """挂起态重启后要重新弹确认，而不是自动执行。"""
        steps = [PlanStep(goal="危险动作", tool="files.list")]
        controller = self._controller(steps)
        state = AgentState(goal="危险动作", task_id="t-resume-ask")
        state.load_plan(steps)
        state.state = TaskState.AWAITING_USER_CONFIRMATION
        state.steps[0].status = StepStatus.AWAITING
        self.store.save(state)

        self.log.clear()
        result = controller.resume_from("t-resume-ask")
        self.assertIsNotNone(result)
        self.assertTrue(result.options, "挂起态续跑应当给出选项让用户重新确认")
        self.assertNotIn("files.list", self.log, "挂起态续跑不该自动执行")
        self.assertTrue(controller.is_waiting)


if __name__ == "__main__":
    unittest.main()
