"""任务收尾回流记忆的测试。

起因：agent 目录**零引用** memory——三层记忆、装配器、检索器都齐备，
但只服务 chat 路径；agent 跑完一个任务，什么都没留下。
下次问「我昨天让你整理过什么」是答不上来的。

这块是**锦上添花**，不是硬标准（任务短、目标自足），所以做得克制：

1. **写 episode 不写 fact**——任务日志进语义层会污染检索，
   以后问「我喜欢什么」可能捞出一堆「用户执行过 X 任务」
2. **只在真正做完时写**——取消/失败的结果没有复用价值，写进去只会污染
3. **best-effort**——记忆是副线，不能把收尾这条主线拖垮
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent.controller import AgentController
from screen_agent.agent.policy import PermissionMode, PolicyEngine
from screen_agent.agent.state import PlanStep
from screen_agent.agent.trajectory import TrajectoryStore
from screen_agent.memory.store import MemoryStoreV2
from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec


class _FakePlanner:
    def __init__(self, steps: list[PlanStep]) -> None:
        self._steps = steps

    def plan(self, goal: str) -> list[PlanStep]:  # noqa: ARG002
        return list(self._steps)

    def looks_like_task(self, text: str) -> bool:  # noqa: ARG002
        return True


def _tool(name: str = "demo.a") -> ToolSpec:
    return ToolSpec(name, "演示工具",
                    lambda **params: ActionResult(success=True, message=f"{name} 完成"),
                    RiskLevel.LOW, idempotent=True)


class EpisodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MemoryStoreV2(Path(self._tmp.name) / "memory.db")
        self.trajectory = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def _controller(self, steps: list[PlanStep], sink=None) -> AgentController:  # noqa: ANN001
        registry = ToolRegistry()
        registry.register(_tool())
        return AgentController(
            registry=registry, planner=_FakePlanner(steps),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.trajectory,
            episode_sink=sink if sink is not None else self.memory.save_agent_episode,
        )

    def test_finished_task_writes_episode(self) -> None:
        controller = self._controller([PlanStep(goal="整理桌面", tool="demo.a")])
        controller.run("整理桌面")

        events = self.memory.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].page_category, "桌面任务")
        self.assertEqual(events[0].user_action, "task_runner")

    def test_episode_carries_task_id_for_traceability(self) -> None:
        """episode 要能溯源回具体任务——否则日后想查「那次到底做了什么」就断了。"""
        controller = self._controller([PlanStep(goal="整理桌面", tool="demo.a")])
        controller.run("整理桌面")

        event = self.memory.list_events()[0]
        self.assertIn(controller.state.task_id, event.evidence_paths)

    def test_episode_is_recorded_as_episode_not_fact(self) -> None:
        """写的是 episode（情节），**不碰语义层**——避免污染事实检索。"""
        controller = self._controller([PlanStep(goal="整理桌面", tool="demo.a")])
        controller.run("整理桌面")

        self.assertEqual(self.memory.list_facts(), [], "任务日志不该写进 facts")

    def test_summary_is_truncated(self) -> None:
        """摘要截到 120 字——记忆里塞长文本只会把检索搞糊。"""
        self.memory.save_agent_episode(
            goal="x", summary="很长" * 200, task_id="t-long",
        )
        event = self.memory.list_events()[0]
        self.assertLessEqual(len(event.summary), 120)

    def test_no_sink_is_fine(self) -> None:
        """没接记忆库时整条链路照常工作。"""
        controller = self._controller([PlanStep(goal="干活", tool="demo.a")], sink=None)
        result = controller.run("干活")
        self.assertTrue(result.success)

    def test_sink_failure_does_not_break_finish(self) -> None:
        """记忆写入失败**不该影响任务收尾**——副线不能拖垮主线。"""

        def _boom(**kwargs):  # noqa: ANN003, ARG001
            raise RuntimeError("记忆库炸了")

        controller = self._controller([PlanStep(goal="干活", tool="demo.a")], sink=_boom)
        result = controller.run("干活")
        self.assertTrue(result.success, "记忆写入失败却把任务收尾也带崩了")

    def test_cancelled_task_writes_nothing(self) -> None:
        """取消的任务不写 episode——没有复用价值，写进去只会污染检索。"""
        controller = self._controller([PlanStep(goal="干活", tool="demo.a")])
        controller.run("干活")
        before = len(self.memory.list_events())

        # 再造一个任务，挂起后取消
        spec = ToolSpec("danger.act", "危险动作",
                        lambda **p: ActionResult(success=True, message="done"),
                        RiskLevel.HIGH)
        registry = ToolRegistry()
        registry.register(spec)
        controller2 = AgentController(
            registry=registry, planner=_FakePlanner([PlanStep(goal="危险", tool="danger.act")]),
            policy=PolicyEngine(mode=PermissionMode.SMART),
            trajectory=self.trajectory,
            episode_sink=self.memory.save_agent_episode,
        )
        controller2.run("危险")
        controller2.cancel()

        self.assertEqual(len(self.memory.list_events()), before,
                         "取消的任务不该写 episode")

    def test_failed_task_still_writes(self) -> None:
        """工具失败但任务收尾了 → 仍然写。

        因为「试过什么、结果如何」本身就是有价值的情节，
        和「任务被取消」不是一回事。
        """
        spec = ToolSpec("fail.tool", "会失败",
                        lambda **p: ActionResult(success=False, message="失败了"),
                        RiskLevel.LOW, idempotent=False)
        registry = ToolRegistry()
        registry.register(spec)
        controller = AgentController(
            registry=registry, planner=_FakePlanner([PlanStep(goal="干活", tool="fail.tool")]),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.trajectory,
            episode_sink=self.memory.save_agent_episode,
        )
        controller.run("干活")
        self.assertEqual(len(self.memory.list_events()), 1)


class StoreApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MemoryStoreV2(Path(self._tmp.name) / "memory.db")

    def test_episode_is_retrievable_by_existing_retriever(self) -> None:
        """复用 events 表的关键收益：**现成的检索器直接就能捞到，零新增检索代码**。"""
        self.memory.save_agent_episode(
            goal="整理下载目录", summary="整理下载目录：把 32 个文件按类型归档",
            task_id="t-abc", started_at=datetime.now(), ended_at=datetime.now(),
        )
        events = self.memory.list_events()
        self.assertEqual(len(events), 1)
        self.assertIn("整理下载目录", events[0].summary)

    def test_repeat_write_same_task_is_idempotent(self) -> None:
        """同一个 task_id 重复写不会堆多条（event_id 是主键，走 INSERT OR REPLACE）。"""
        for _ in range(3):
            self.memory.save_agent_episode(goal="干同一件事", summary="结果一样", task_id="t-1")
        self.assertEqual(len(self.memory.list_events()), 1)


if __name__ == "__main__":
    unittest.main()
