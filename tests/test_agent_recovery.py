"""错误恢复的测试。

改这个的起因：工具失败**只记不救**——记 FAILED 然后继续，没有任何重试、
降级或重规划。业界的一致做法是「先分级、再定策略」，三条支柱：
幂等 + 有预算的重试 + 补偿（补偿这块对这个项目不适用，见下）。

所以这组测试要守住：

1. **只有瞬时错误才重试**——参数错、权限错、超时未知都重试不了
2. **只有幂等工具才重试**——写操作重跑会再来一次，宁可少救
3. **重试有预算**，不是"重到成功"（那会变成重试风暴）
4. **同一个工具反复挂要熔断**——那通常说明路走不通，不是运气不好

明确不做补偿（saga）：文件操作已经有 `files.undo` + 回收站兜底，
再搭一套补偿事务框架是过度工程。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent.controller import AgentController
from screen_agent.agent.policy import PermissionMode, PolicyEngine
from screen_agent.agent.state import PlanStep, StepStatus
from screen_agent.agent.trajectory import TrajectoryStore
from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec


class _FakePlanner:
    """可切换计划的规划器，用来测 replan。"""

    def __init__(self, plans: list[list[PlanStep]]) -> None:
        self._plans = plans
        self.calls = 0

    def plan(self, goal: str) -> list[PlanStep]:  # noqa: ARG002
        index = min(self.calls, len(self._plans) - 1)
        self.calls += 1
        return list(self._plans[index])

    def looks_like_task(self, text: str) -> bool:  # noqa: ARG002
        return True


def _flaky(name: str, *, fail_times: int, error_kind: str = "retryable",
           idempotent: bool = True, calls: list[str] | None = None) -> ToolSpec:
    """前 fail_times 次失败，之后成功。"""
    state = {"n": 0}

    def _handler(**params) -> ActionResult:  # noqa: ARG001
        if calls is not None:
            calls.append(name)
        state["n"] += 1
        if state["n"] <= fail_times:
            return ActionResult(success=False, message=f"{name} 暂时失败",
                                error_kind=error_kind)
        return ActionResult(success=True, message=f"{name} 成功")

    return ToolSpec(name, f"不稳定工具 {name}", _handler, RiskLevel.LOW,
                    idempotent=idempotent)


class RetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def _build(self, specs: list[ToolSpec], steps: list[PlanStep],
               **kwargs) -> AgentController:  # noqa: ANN003
        registry = ToolRegistry()
        for spec in specs:
            registry.register(spec)
        return AgentController(
            registry=registry, planner=_FakePlanner([steps]),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.store, **kwargs,
        )

    def test_retry_on_retryable_idempotent(self) -> None:
        """瞬时错误 + 幂等 → 重试到成功。"""
        calls: list[str] = []
        spec = _flaky("net.fetch", fail_times=2, calls=calls)
        controller = self._build([spec], [PlanStep(goal="拉数据", tool="net.fetch")])

        result = controller.run("拉数据")
        self.assertTrue(result.success)
        self.assertEqual(len(calls), 3, "两次失败后第三次成功")

    def test_no_retry_on_nonidempotent(self) -> None:
        """非幂等写操作失败 → **只执行一次**，绝不自动重试。"""
        calls: list[str] = []
        spec = _flaky("files.move", fail_times=1, idempotent=False, calls=calls)
        controller = self._build([spec], [PlanStep(goal="移文件", tool="files.move")])

        controller.run("移文件")
        self.assertEqual(len(calls), 1, "写操作被重试了——可能产生第二次副作用")

    def test_no_retry_on_correctable(self) -> None:
        """参数/前置条件问题重试没用，该换条路（交给 replan）。"""
        calls: list[str] = []
        spec = _flaky("files.read", fail_times=1, error_kind="correctable", calls=calls)
        controller = self._build([spec], [PlanStep(goal="读文件", tool="files.read")])

        controller.run("读文件")
        self.assertEqual(len(calls), 1, "可修正错误不该靠重试解决")

    def test_no_retry_on_fatal(self) -> None:
        # 注意别用 sys.lock 这类名字：它们在 `_DESTRUCTIVE_TOOLS` 里，
        # AUTO 模式下也会被挂起等确认，压根执行不到，测不出重试行为
        calls: list[str] = []
        spec = _flaky("env.check", fail_times=1, error_kind="fatal", calls=calls)
        controller = self._build([spec], [PlanStep(goal="检查环境", tool="env.check")])

        controller.run("检查环境")
        self.assertEqual(len(calls), 1)

    def test_retry_budget_per_step(self) -> None:
        """单步重试有上限——不是「重到成功」。"""
        calls: list[str] = []
        spec = _flaky("net.fetch", fail_times=99, calls=calls)
        controller = self._build(
            [spec], [PlanStep(goal="拉数据", tool="net.fetch")],
            max_retries_per_step=2,
        )

        controller.run("拉数据")
        self.assertEqual(len(calls), 3, "初次 + 2 次重试")

    def test_retry_budget_total(self) -> None:
        """任务级总预算，防重试风暴。"""
        calls: list[str] = []
        spec = _flaky("net.fetch", fail_times=99, calls=calls)
        steps = [PlanStep(goal=f"第{i}步", tool="net.fetch") for i in range(4)]
        controller = self._build(
            [spec], steps, max_retries_per_step=5, max_total_retries=2,
        )

        controller.run("多步任务")
        # 4 步各跑 1 次 = 4，加总预算 2 次重试 = 最多 6
        self.assertLessEqual(len(calls), 6, "任务级重试预算没生效")

    def test_success_needs_no_retry(self) -> None:
        calls: list[str] = []
        spec = _flaky("net.fetch", fail_times=0, calls=calls)
        controller = self._build([spec], [PlanStep(goal="拉数据", tool="net.fetch")])
        controller.run("拉数据")
        self.assertEqual(len(calls), 1)


class BreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def test_breaker_skips_after_repeated_failures(self) -> None:
        """同一工具反复挂 → 后续直接跳过，不再浪费时间。

        注意每步给**不同的参数**：参数全一样的话会先被「打转检测」拦下
        （那个是按签名去重的），就测不到熔断了。
        """
        calls: list[str] = []
        spec = _flaky("flaky.tool", fail_times=99, idempotent=False, calls=calls)
        steps = [PlanStep(goal=f"第{i}步", tool="flaky.tool", params={"n": i})
                 for i in range(6)]
        registry = ToolRegistry()
        registry.register(spec)
        controller = AgentController(
            registry=registry, planner=_FakePlanner([steps]),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.store, breaker_threshold=3,
        )

        controller.run("一直失败的任务")
        self.assertLessEqual(len(calls), 3, "熔断后不该再继续尝试")
        skipped = [s for s in controller.state.steps
                   if s.status is StepStatus.SKIPPED and "反复失败" in s.observation]
        self.assertTrue(skipped, "熔断的步骤应当被明确标成跳过并说明原因")


class ReplanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def test_replan_on_correctable(self) -> None:
        """可修正的失败 → 换条路走一次。"""
        calls: list[str] = []
        broken = _flaky("files.read", fail_times=99, error_kind="correctable", calls=calls)
        backup = _flaky("files.glob", fail_times=0, calls=calls)

        original = [PlanStep(goal="读那个文件", tool="files.read")]
        replanned = [PlanStep(goal="换个方式找文件", tool="files.glob")]
        planner = _FakePlanner([original, replanned])

        registry = ToolRegistry()
        registry.register(broken)
        registry.register(backup)
        controller = AgentController(
            registry=registry, planner=planner,
            policy=PolicyEngine(mode=PermissionMode.AUTO), trajectory=self.store,
        )

        controller.run("读文件")
        self.assertIn("files.glob", calls, "可修正失败后应当换条路走")
        self.assertEqual(planner.calls, 2, "应当只剩一次重规划（默认上限 1）")

    def test_replan_removes_failed_signature(self) -> None:
        """重规划要剔除与失败步同签名的步骤，否则规划器多半给出同一套。

        这里只能断言「原步骤没被替换」——`plan()` 调用本身躲不掉
        （得先拿到新计划才能过滤），所以别去数调用次数。
        """
        calls: list[str] = []
        broken = _flaky("files.read", fail_times=99, error_kind="correctable", calls=calls)

        same = [PlanStep(goal="再读一次", tool="files.read")]
        planner = _FakePlanner([same, same])   # 重规划还是给出同一套
        registry = ToolRegistry()
        registry.register(broken)
        controller = AgentController(
            registry=registry, planner=planner,
            policy=PolicyEngine(mode=PermissionMode.AUTO), trajectory=self.store,
        )

        controller.run("读文件")
        self.assertEqual(len(controller.state.steps), 1, "同签名的新计划不该被采纳")
        self.assertEqual(controller.state.steps[0].tool, "files.read")


if __name__ == "__main__":
    unittest.main()
