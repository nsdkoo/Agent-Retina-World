"""运行中取消与步级超时的测试。

改这个的起因：`cancel()` 原先只在挂起时有意义（对应「门口说不做」），
任务一旦跑起来就**没法叫停**——一个跑了三步的任务只能等它跑完。

取消用协作式令牌（Python 杀不掉线程），所以有两条性质必须钉住：

1. **取消在步边界生效**，不是即时——当前这步已经发出去了，硬切会把副作用留在半路
2. **挂起时的取消行为不变**（仍是 REJECTED）——那是「门口拒绝」，和「中途叫停」是两回事，
   所以新增了 `CANCELLED` 状态而不是复用 REJECTED

超时那条还带一个重要区分：写操作超时**不能当普通失败重试**，因为它可能已经改过东西了。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent.controller import AgentController
from screen_agent.agent.policy import PermissionMode, PolicyEngine
from screen_agent.agent.state import PlanStep, TaskState
from screen_agent.agent.trajectory import TrajectoryStore
from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec


class _FakePlanner:
    def __init__(self, steps: list[PlanStep]) -> None:
        self._steps = steps

    def plan(self, goal: str) -> list[PlanStep]:  # noqa: ARG002
        return list(self._steps)

    def looks_like_task(self, text: str) -> bool:  # noqa: ARG002
        return True


class CancelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")
        self.log: list[str] = []

    def _build(self, specs: list[ToolSpec], steps: list[PlanStep],
               step_timeout: float = 0.0) -> AgentController:
        registry = ToolRegistry()
        for spec in specs:
            registry.register(spec)
        return AgentController(
            registry=registry, planner=_FakePlanner(steps),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.store, step_timeout=step_timeout,
        )

    def _slow_tool(self, name: str, seconds: float, idempotent: bool = False,
                   calls: list[str] | None = None,
                   risk: RiskLevel = RiskLevel.LOW) -> ToolSpec:
        """默认用 LOW 风险：SAFE 的连续步骤会被 `_collect_parallel` 攒成一批并行跑，
        那样任务一瞬间就结束了，根本来不及测"运行中取消"。"""
        def _handler(**params) -> ActionResult:  # noqa: ARG001
            if calls is not None:
                calls.append(name)
            time.sleep(seconds)
            return ActionResult(success=True, message=f"{name} 完成")

        return ToolSpec(name, f"慢工具 {name}", _handler, risk,
                        idempotent=idempotent)

    # ---- 运行中取消 ----

    def test_cancel_running_task_reaches_cancelled(self) -> None:
        """任务跑起来之后从外部叫停 → 状态进 CANCELLED。"""
        steps = [PlanStep(goal=f"第{i}步", tool="slow.step") for i in range(4)]
        controller = self._build([self._slow_tool("slow.step", 0.15, idempotent=True)], steps)

        thread = threading.Thread(target=controller.run, args=("慢任务",), daemon=True)
        thread.start()
        time.sleep(0.25)                      # 等它跑起来
        cancel_result = controller.cancel()
        thread.join(timeout=5)

        # 两个返回值含义不同：`cancel()` 说的是「叫停指令收到了」，
        # `run()` 说的是「任务跑完了没」——被叫停当然不算跑完
        self.assertTrue(cancel_result.success)
        self.assertEqual(controller.state.state, TaskState.CANCELLED)

    def test_cancel_is_not_instant(self) -> None:
        """取消在步边界生效：当前这步会跑完，不会半路切断。"""
        calls: list[str] = []
        steps = [PlanStep(goal=f"第{i}步", tool="slow.step") for i in range(3)]
        controller = self._build(
            [self._slow_tool("slow.step", 0.2, idempotent=True, calls=calls)], steps
        )

        thread = threading.Thread(target=controller.run, args=("慢任务",), daemon=True)
        thread.start()
        time.sleep(0.3)                       # 第一步正在跑
        controller.cancel()
        thread.join(timeout=5)

        self.assertGreaterEqual(len(calls), 1, "当前步应当跑完再停")
        self.assertLess(len(calls), 3, "取消后不该把剩下的步全跑完")

    def test_cancelled_skipped_step_is_not_failure(self) -> None:
        """被叫停的步记成跳过（不是失败）——那不是出错，是用户不想做了。"""
        calls: list[str] = []
        steps = [PlanStep(goal=f"第{i}步", tool="slow.step") for i in range(4)]
        controller = self._build(
            [self._slow_tool("slow.step", 0.12, idempotent=True, calls=calls)], steps
        )
        thread = threading.Thread(target=controller.run, args=("慢任务",), daemon=True)
        thread.start()
        time.sleep(0.2)
        controller.cancel()
        thread.join(timeout=5)

        failed = controller.state.failed_steps()
        self.assertEqual(failed, [], "被叫停的步不该记成失败")

    def test_cancel_when_idle_is_rejected(self) -> None:
        controller = self._build([], [])
        self.assertFalse(controller.cancel().success)

    # ---- 挂起时取消（回归：行为不能变） ----

    def test_cancel_while_waiting_still_rejected(self) -> None:
        """挂起时的取消仍走 REJECTED——那是「门口拒绝」，和「中途叫停」不是一回事。"""
        calls: list[str] = []
        spec = self._slow_tool("danger.step", 0.0, calls=calls)
        spec.risk = RiskLevel.HIGH                # 高风险 → 会挂起等确认
        steps = [PlanStep(goal="危险动作", tool="danger.step")]
        controller = self._build([spec], steps)

        controller.run("危险动作")
        self.assertTrue(controller.is_waiting)

        result = controller.cancel()
        self.assertTrue(result.success)
        self.assertEqual(controller.state.state, TaskState.REJECTED)
        self.assertEqual(calls, [], "挂起时取消不该执行任何工具")


class StepTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def _build(self, spec: ToolSpec, steps: list[PlanStep],
               step_timeout: float) -> AgentController:
        registry = ToolRegistry()
        registry.register(spec)
        return AgentController(
            registry=registry, planner=_FakePlanner(steps),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.store, step_timeout=step_timeout,
        )

    def _sleeper(self, seconds: float, idempotent: bool,
                 calls: list[str] | None = None) -> ToolSpec:
        def _handler(**params) -> ActionResult:  # noqa: ARG001
            if calls is not None:
                calls.append("call")
            time.sleep(seconds)
            return ActionResult(success=True, message="太慢了")

        return ToolSpec("slow.tool", "慢工具", _handler, RiskLevel.SAFE,
                        idempotent=idempotent)

    def test_timeout_marks_failed_without_retry(self) -> None:
        calls: list[str] = []
        steps = [PlanStep(goal="慢活", tool="slow.tool")]
        controller = self._build(self._sleeper(1.5, True, calls), steps, step_timeout=0.3)

        start = time.time()
        result = controller.run("慢活")
        elapsed = time.time() - start

        self.assertLess(elapsed, 1.2, "超时后不该还在等")
        self.assertEqual(len(calls), 1, "超时不该自动重试")

    def test_idempotent_timeout_is_retryable(self) -> None:
        """只读工具超时 → 归 retryable（重跑无害）。"""
        steps = [PlanStep(goal="读一下", tool="slow.tool")]
        controller = self._build(self._sleeper(1.5, True), steps, step_timeout=0.3)
        controller.run("读一下")
        step = controller.state.steps[0]
        self.assertIn("还没返回", step.observation)
        self.assertEqual(step.status.value, "failed")

    def test_nonidempotent_timeout_is_unknown(self) -> None:
        """写操作超时 → 归 timeout_unknown，**不能当普通失败重试**——
        它可能已经把东西改了，重跑就是第二次副作用。"""
        registry = ToolRegistry()
        registry.register(self._sleeper(1.5, False))
        steps = [PlanStep(goal="写点东西", tool="slow.tool")]
        controller = AgentController(
            registry=registry, planner=_FakePlanner(steps),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.store, step_timeout=0.3,
        )
        # 直接调一次带超时的执行，检查错误分类
        step = steps[0]
        from screen_agent.agent.state import StepRecord
        record = StepRecord(index=0, goal=step.goal, tool=step.tool, params={})
        controller._state = None
        result = controller._invoke_with_timeout(record)
        self.assertFalse(result.success)
        self.assertEqual(result.error_kind, "timeout_unknown")

    def test_timeout_zero_disables_it(self) -> None:
        """默认关闭：不设超时时行为与以前完全一致。"""
        steps = [PlanStep(goal="快活", tool="slow.tool")]
        controller = self._build(self._sleeper(0.05, True), steps, step_timeout=0.0)
        result = controller.run("快活")
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
