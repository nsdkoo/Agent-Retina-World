"""记忆接进决策的测试（差异化第一步：懂你）。

改这个的起因：三层记忆、装配器、检索器全都齐备，但**只服务 chat 路径**——
`agent/` 目录零引用 `memory`。也就是说它辛辛苦苦记了一大堆，
**干活的时候一次都不看**。加上任务收尾回流之后变成了"只写不读"。

这一组要守住：

1. **规划真的会读记忆**——`Planner._prompt` 里出现先例片段，而且只走 LLM 路径
2. **记忆取不到时不能拖垮规划**——记忆是副线，规划是主线
3. **预算要小**——planner prompt 里已经有一长串工具清单，记忆塞多了会挤掉工具
4. **先例要带「手法」**——只说"做了什么"没用，得说"用了什么工具"，
   否则下次遇到类似目标时规划器还是不知道该怎么拆
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent.controller import AgentController
from screen_agent.agent.planner import Planner
from screen_agent.agent.policy import PermissionMode, PolicyEngine
from screen_agent.agent.state import PlanStep, StepStatus
from screen_agent.agent.trajectory import TrajectoryStore
from screen_agent.memory.assembler import ContextAssembler
from screen_agent.memory.store import MemoryStoreV2
from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec


class _EchoClient:
    """假 chat client，把收到的 prompt 存下来供断言。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, messages, system=None):  # noqa: ANN001, ARG002
        self.prompts.append(messages[0]["content"])
        return "[]"


class _FakePlanner:
    def __init__(self, steps: list[PlanStep]) -> None:
        self._steps = steps

    def plan(self, goal: str) -> list[PlanStep]:  # noqa: ARG002
        return list(self._steps)

    def looks_like_task(self, text: str) -> bool:  # noqa: ARG002
        return True


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec("files.list", "列目录", lambda **p: ActionResult(True, "ok"),
                          RiskLevel.LOW, idempotent=True))
    reg.register(ToolSpec("files.move", "移文件", lambda **p: ActionResult(True, "ok"),
                          RiskLevel.LOW, {"src": "源", "dst": "目标"},
                          schema={"src": {"type": "string", "required": True},
                                  "dst": {"type": "string", "required": True}}))
    reg.register(ToolSpec("files.glob", "找文件", lambda **p: ActionResult(True, "ok"),
                          RiskLevel.LOW, {"pattern": "匹配式"},
                          schema={"pattern": {"type": "string", "required": True}}))
    return reg


class AgentContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MemoryStoreV2(Path(self._tmp.name) / "memory.db")

    def test_seeds_similar_episode_as_precedent(self) -> None:
        """做过类似的事 → 应当作为先例被取出来。"""
        self.memory.save_agent_episode(
            goal="整理下载目录", summary="整理下载目录（用了 files.list、files.move）",
            task_id="t-1",
        )
        snippet = ContextAssembler(self.memory).build_agent_context("帮我整理下载目录")
        self.assertIn("整理下载目录", snippet)

    def test_empty_memory_returns_empty_string(self) -> None:
        """没记忆时返回空串——调用方据此整段跳过，别丢个空标题占 prompt。"""
        self.assertEqual(ContextAssembler(self.memory).build_agent_context("随便干点什么"), "")

    def test_budget_is_respected(self) -> None:
        """预算要小：planner prompt 里工具清单本来就长。"""
        for i in range(20):
            self.memory.save_agent_episode(
                goal=f"任务{i}", summary="很长的摘要内容" * 10, task_id=f"t-{i}",
            )
        snippet = ContextAssembler(self.memory).build_agent_context("任务", budget_tokens=80)
        self.assertLessEqual(len(snippet), 400, "记忆片段超预算了")

    def test_facts_are_included_with_low_confidence_filtered(self) -> None:
        self.memory.add_fact("preference", "用户偏好按项目分文件夹", confidence=0.8)
        self.memory.add_fact("entity", "低置信噪声", confidence=0.1)
        snippet = ContextAssembler(self.memory).build_agent_context("整理文件")
        self.assertIn("按项目分文件夹", snippet)
        self.assertNotIn("低置信噪声", snippet, "低置信的事实不该喂给规划器")


class PlannerContextTests(unittest.TestCase):
    def test_prompt_includes_memory_block(self) -> None:
        client = _EchoClient()
        planner = Planner(_registry(), chat_client=client,
                          context_fn=lambda goal: "- 之前做过：整理下载目录（用了 files.move）")
        planner._prompt("整理下载目录")
        # 直接调 _prompt 更快，但这里也验证一次完整链路
        planner.plan("整理下载目录")
        self.assertTrue(client.prompts, "没有调用 LLM")
        self.assertIn("之前做过", client.prompts[-1])

    def test_prompt_skips_block_when_no_memory(self) -> None:
        """取不到记忆时不该留下「关于这位用户…」这种空标题。"""
        client = _EchoClient()
        planner = Planner(_registry(), chat_client=client, context_fn=lambda goal: "")
        planner.plan("整理下载目录")
        self.assertNotIn("关于这位用户", client.prompts[-1])

    def test_context_fn_failure_does_not_break_planning(self) -> None:
        """记忆出错不能拖垮规划——副线不能牵制主线。"""
        def _boom(goal):  # noqa: ANN001, ARG001
            raise RuntimeError("记忆库炸了")

        client = _EchoClient()
        planner = Planner(_registry(), chat_client=client, context_fn=_boom)
        result = planner.plan("整理下载目录")
        self.assertIsInstance(result, list)
        self.assertTrue(client.prompts, "记忆报错后规划居然没跑")

    def test_no_context_fn_still_works(self) -> None:
        """没接记忆时行为与之前完全一致。"""
        client = _EchoClient()
        planner = Planner(_registry(), chat_client=client)
        planner.plan("整理下载目录")
        self.assertTrue(client.prompts)
        self.assertNotIn("关于这位用户", client.prompts[-1])


class EpisodeRichnessTests(unittest.TestCase):
    """episode 要带上「手法」，否则先例没有参考价值。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MemoryStoreV2(Path(self._tmp.name) / "memory.db")
        self.trajectory = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def _run(self, steps: list[PlanStep]) -> AgentController:
        controller = AgentController(
            registry=_registry(), planner=_FakePlanner(steps),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.trajectory,
            episode_sink=self.memory.save_agent_episode,
        )
        controller.run("整理下载目录")
        return controller

    def test_summary_mentions_tools_used(self) -> None:
        self._run([PlanStep(goal="列目录", tool="files.list", params={"path": "~/Downloads"})])
        event = self.memory.list_events()[0]
        self.assertIn("files.list", event.summary, "摘要里没记用了什么工具")

    def test_resources_are_recorded(self) -> None:
        """动到的路径要记下来——这是将来提炼「习惯」唯一的原材料。"""
        self._run([
            PlanStep(goal="列目录", tool="files.list", params={"path": "~/Downloads"}),
            PlanStep(goal="归类", tool="files.move",
                     params={"src": "~/Downloads/a.zip", "dst": "~/Archive"}),
        ])
        event = self.memory.list_events()[0]
        joined = " ".join(event.evidence_paths)
        self.assertIn("~/Downloads", joined)
        self.assertIn("~/Archive", joined)

    def test_task_id_kept_for_traceability(self) -> None:
        controller = self._run([PlanStep(goal="列目录", tool="files.list")])
        event = self.memory.list_events()[0]
        self.assertIn(controller.state.task_id, event.evidence_paths)


if __name__ == "__main__":
    unittest.main()
