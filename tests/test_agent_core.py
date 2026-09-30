"""Agent 运行时测试：事件流 / 状态机 / 权限策略 / 规划 / 主循环 / 轨迹。

主循环用例全程在临时目录里跑真实工具（把用户目录 patch 到 tmp），删除动作换成假实现，
不碰真实桌面也不污染回收站。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent import build_agent, split_clauses
from screen_agent.agent.events import Event, EventStream, EventType
from screen_agent.agent.planner import Planner
from screen_agent.agent.policy import Decision, PermissionMode, PolicyEngine, ToolPermission
from screen_agent.agent.state import AgentState, IllegalTransition, TaskState
from screen_agent.agent.trajectory import TrajectoryStore
from screen_agent.tools import files
from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel
from screen_agent.tools.registry_setup import build_default_registry


# ---------------------------------------------------------------- 事件流

class EventStreamTests(unittest.TestCase):
    def test_subscribe_and_history(self) -> None:
        stream = EventStream()
        seen: list[str] = []
        stream.subscribe(EventType.ACTION, lambda e: seen.append(e.payload["goal"]))
        stream.publish(Event(EventType.ACTION, {"goal": "a"}))
        stream.publish(Event(EventType.FINISH, {"summary": "x"}))
        self.assertEqual(seen, ["a"])
        self.assertEqual(len(stream.history()), 2)
        self.assertEqual(len(stream.history(EventType.FINISH)), 1)

    def test_wildcard_subscriber(self) -> None:
        stream = EventStream()
        count = []
        stream.subscribe(None, lambda e: count.append(e.type))
        stream.publish(Event(EventType.PLAN, {}))
        stream.publish(Event(EventType.ASK, {}))
        self.assertEqual(count, [EventType.PLAN, EventType.ASK])

    def test_broken_subscriber_does_not_break_publish(self) -> None:
        stream = EventStream()
        stream.subscribe(None, lambda e: 1 / 0)
        ok = []
        stream.subscribe(None, lambda e: ok.append(1))
        stream.publish(Event(EventType.PLAN, {}))
        self.assertEqual(ok, [1])

    def test_unsubscribe(self) -> None:
        stream = EventStream()
        seen = []
        off = stream.subscribe(EventType.PLAN, lambda e: seen.append(1))
        off()
        stream.publish(Event(EventType.PLAN, {}))
        self.assertEqual(seen, [])


# ---------------------------------------------------------------- 状态机

class StateMachineTests(unittest.TestCase):
    def test_legal_flow(self) -> None:
        state = AgentState(goal="g")
        state.transition(TaskState.PLANNING)
        state.transition(TaskState.RUNNING)
        state.transition(TaskState.AWAITING_USER_CONFIRMATION)
        state.transition(TaskState.RUNNING)
        state.finish("搞定")
        self.assertEqual(state.state, TaskState.FINISHED)
        self.assertEqual(state.result, "搞定")

    def test_illegal_transition_raises(self) -> None:
        state = AgentState(goal="g")
        with self.assertRaises(IllegalTransition):
            state.transition(TaskState.FINISHED)  # 还没跑就想结束

    def test_finished_is_terminal(self) -> None:
        state = AgentState(goal="g")
        state.transition(TaskState.PLANNING)
        state.transition(TaskState.RUNNING)
        state.finish("done")
        with self.assertRaises(IllegalTransition):
            state.transition(TaskState.RUNNING)


# ---------------------------------------------------------------- 权限策略

class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_default_registry()
        self.safe = self.registry.get("files.list")
        self.low = self.registry.get("files.organize")
        self.high = self.registry.get("files.delete")

    def test_smart_mode_by_risk(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.SMART)
        self.assertIs(policy.judge(self.safe, {})[0], Decision.ALLOW)
        self.assertIs(policy.judge(self.low, {})[0], Decision.ALLOW)
        self.assertIs(policy.judge(self.high, {})[0], Decision.ASK)

    def test_chat_mode_denies_everything(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.CHAT)
        self.assertIs(policy.judge(self.safe, {})[0], Decision.DENY)

    def test_approve_mode_asks_on_write(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.APPROVE)
        self.assertIs(policy.judge(self.safe, {})[0], Decision.ALLOW)  # 只读不打扰
        self.assertIs(policy.judge(self.low, {})[0], Decision.ASK)

    def test_auto_mode_still_asks_high(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.AUTO)
        self.assertIs(policy.judge(self.low, {})[0], Decision.ALLOW)
        self.assertIs(policy.judge(self.high, {})[0], Decision.ASK)

    def test_auto_mode_can_be_opened_up(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.AUTO, auto_allows_high=True)
        self.assertIs(policy.judge(self.high, {})[0], Decision.ALLOW)

    def test_override_beats_mode(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.SMART)
        policy.remember("files.list", ToolPermission.NEVER_ALLOW)
        self.assertIs(policy.judge(self.safe, {})[0], Decision.DENY)
        policy.remember("files.delete", ToolPermission.ALWAYS_ALLOW)
        self.assertIs(policy.judge(self.high, {})[0], Decision.ALLOW)

    def test_destructive_params_force_ask(self) -> None:
        policy = PolicyEngine(mode=PermissionMode.AUTO, auto_allows_high=True)
        decision, reason = policy.judge(self.low, {"path": "删除全部"})
        self.assertIs(decision, Decision.ASK)
        self.assertTrue(reason)

    def test_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            policy = PolicyEngine(mode=PermissionMode.APPROVE, store_path=path)
            policy.remember("files.list", ToolPermission.ALWAYS_ALLOW)
            reloaded = PolicyEngine(store_path=path)
            self.assertIs(reloaded.mode, PermissionMode.APPROVE)
            self.assertIs(reloaded.judge(self.safe, {})[0], Decision.ALLOW)


# ---------------------------------------------------------------- 规划器

class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_default_registry()
        self.planner = Planner(self.registry)

    def test_split_clauses(self) -> None:
        self.assertEqual(
            split_clauses("整理桌面，然后把截图移动到文档"),
            ["整理桌面", "把截图移动到文档"],
        )

    def test_rule_plan_on_compound_command(self) -> None:
        steps = self.planner.plan("新建文件夹 素材 然后 把 桌面/a.pdf 移动到 素材")
        self.assertEqual([s.tool for s in steps], ["files.mkdir", "files.move"])

    def test_compound_command_is_detected_as_task(self) -> None:
        # 单步规则的正则会贪婪吃掉整句，这里必须仍然判成任务
        self.assertTrue(self.planner.looks_like_task("新建文件夹 素材 然后 把 桌面/a.pdf 移动到 素材"))

    def test_single_command_is_not_task(self) -> None:
        self.assertFalse(self.planner.looks_like_task("整理桌面"))
        self.assertFalse(self.planner.looks_like_task("你好啊"))

    def test_partial_unparsable_clause_gives_up(self) -> None:
        self.assertEqual(self.planner.plan("整理桌面，然后请你吃饭"), [])

    def test_llm_plan_validation(self) -> None:
        raw = """```json
        [
          {"goal": "建目录", "tool": "files.mkdir", "params": {"path": "素材"}, "why": "要先有目录"},
          {"goal": "瞎编", "tool": "not.a.tool", "params": {}},
          {"goal": "危险", "tool": "files.delete", "params": {"path": "x"}},
          {"goal": "多余参数", "tool": "files.list", "params": {"path": "~", "不存在的键": 1}}
        ]
        ```"""

        class _Fake:
            def complete(self, messages, system=None):  # noqa: ANN001
                return raw

        planner = Planner(self.registry, chat_client=_Fake(), max_risk=RiskLevel.LOW)
        steps = planner.plan("随便一个规则拆不动的多步目标")
        self.assertEqual([s.tool for s in steps], ["files.mkdir", "files.list"])
        self.assertEqual(steps[1].params, {"path": "~"})  # 不认识的参数被剔掉

    def test_llm_plan_single_step_rejected(self) -> None:
        class _Fake:
            def complete(self, messages, system=None):  # noqa: ANN001
                return '[{"goal": "x", "tool": "files.list", "params": {}}]'

        planner = Planner(self.registry, chat_client=_Fake())
        self.assertEqual(planner.plan("规则拆不动的目标啊"), [])


# ---------------------------------------------------------------- 轨迹

class TrajectoryTests(unittest.TestCase):
    def test_save_load_and_unfinished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TrajectoryStore(Path(tmp) / "tasks.db")
            state = AgentState(goal="整理桌面")
            state.transition(TaskState.PLANNING)
            state.transition(TaskState.RUNNING)
            store.save(state)
            loaded = store.load(state.task_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.goal, "整理桌面")
            self.assertEqual(loaded.state, TaskState.RUNNING)
            self.assertIsNotNone(store.latest_unfinished())
            state.finish("done")
            store.save(state)
            self.assertIsNone(store.latest_unfinished())
            self.assertEqual(len(store.recent()), 1)


# ---------------------------------------------------------------- 主循环

class AgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        (self.root / "Downloads").mkdir()

        for name, fn in (
            ("_user_dir", lambda folder: self.root / folder),
            ("_journal_path", lambda: self.root / "journal.json"),
            ("_pending_path", lambda: self.root / "pending.json"),
            # 删除会真送回收站，这里换掉，测试不污染系统
            ("delete_path", lambda path: ActionResult(success=True, message=f"已放进回收站：{path}")),
        ):
            patcher = patch.object(files, name, side_effect=fn) if name == "_user_dir" \
                else patch.object(files, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

        (self.desktop / "报告.pdf").write_text("x", encoding="utf-8")
        (self.desktop / "旧图.png").write_bytes(b"x")

    def _agent(self, **kwargs):
        return build_agent(self.root, **kwargs)

    def test_two_step_task_runs(self) -> None:
        agent = self._agent()
        result = agent.run("新建文件夹 素材 然后 把 桌面/报告.pdf 移动到 素材")
        self.assertTrue(result.success, result.message)
        self.assertTrue((self.desktop / "素材" / "报告.pdf").exists())
        self.assertEqual(agent.state.state, TaskState.FINISHED)
        self.assertEqual(agent.state.done_count(), 2)

    def test_progress_events_emitted(self) -> None:
        agent = self._agent()
        kinds = []
        agent.stream.subscribe(None, lambda e: kinds.append(e.type))
        agent.run("新建文件夹 素材 然后 把 桌面/报告.pdf 移动到 素材")
        self.assertIn(EventType.PLAN, kinds)
        self.assertIn(EventType.ACTION, kinds)
        self.assertIn(EventType.OBSERVATION, kinds)
        self.assertIn(EventType.FINISH, kinds)

    def test_task_persisted_to_trajectory(self) -> None:
        agent = self._agent()
        result = agent.run("新建文件夹 素材 然后 把 桌面/报告.pdf 移动到 素材")
        task_id = result.detail["task_id"]
        loaded = agent.trajectory.load(task_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.done_count(), 2)
        self.assertEqual(len(agent.trajectory.recent()), 1)

    def test_high_risk_step_suspends_then_confirms(self) -> None:
        agent = self._agent()
        first = agent.run("删除 桌面/报告.pdf 然后 删除 桌面/旧图.png")
        self.assertTrue(agent.is_waiting)
        self.assertIn("继续执行", first.options)

        second = agent.try_resume("继续执行")
        self.assertTrue(agent.is_waiting)          # 第 2 步又要确认
        self.assertIn("继续执行", second.options)

        third = agent.try_resume("跳过这一步")
        self.assertFalse(agent.is_waiting)
        self.assertEqual(agent.state.state, TaskState.FINISHED)
        self.assertTrue(third.message)

    def test_cancel_rejects_task(self) -> None:
        agent = self._agent()
        agent.run("删除 桌面/报告.pdf 然后 删除 桌面/旧图.png")
        result = agent.try_resume("取消任务")
        self.assertTrue(result.success)
        self.assertEqual(agent.state.state, TaskState.REJECTED)
        self.assertFalse(agent.is_waiting)

    def test_steering_abandons_task(self) -> None:
        """说别的就算改变方向：任务停掉，控制权交回上层（Pi 的 steering 语义）。"""
        agent = self._agent()
        agent.run("删除 桌面/报告.pdf 然后 删除 桌面/旧图.png")
        self.assertIsNone(agent.try_resume("打开百度"))
        self.assertFalse(agent.is_waiting)
        self.assertEqual(agent.state.state, TaskState.STOPPED)

    def test_never_allow_skips_step(self) -> None:
        agent = self._agent()
        agent.policy.remember("files.mkdir", ToolPermission.NEVER_ALLOW)
        agent.run("新建文件夹 素材 然后 把 桌面/报告.pdf 移动到 素材")
        self.assertEqual(agent.state.steps[0].status.value, "skipped")

    def test_repeated_step_is_skipped(self) -> None:
        """同一动作来回做说明计划坏了，Pi 式打转检测直接跳过。"""
        agent = self._agent()

        class _LoopPlanner:
            def plan(self, goal):  # noqa: ANN001
                from screen_agent.agent.state import PlanStep

                return [PlanStep("列出桌面", "files.list", {"path": "桌面"})] * 3

            def looks_like_task(self, text):  # noqa: ANN001
                return True

        from screen_agent.agent.controller import AgentController

        controller = AgentController(agent.registry, _LoopPlanner(), agent.policy)  # type: ignore[arg-type]
        controller.max_step_repeats = 1
        controller.run("重复三次的假任务")
        statuses = [s.status.value for s in controller.state.steps]
        self.assertEqual(statuses.count("done"), 1)
        self.assertEqual(statuses.count("skipped"), 2)

    def test_unplannable_goal_returns_helpful_message(self) -> None:
        agent = self._agent()
        result = agent.run("给我讲个笑话")
        self.assertFalse(result.success)
        self.assertIn("没拆成", result.message)
        self.assertEqual(agent.state.state, TaskState.ERROR)

    def test_follow_up_queue(self) -> None:
        agent = self._agent()
        agent.follow_up("新建文件夹 备份")
        self.assertEqual(len(agent._follow_ups), 1)


class _EchoExecutor:
    """假的单步执行器：只回声，用来验证 assistant 的分流没错。"""

    def run(self, intent):  # noqa: ANN001
        return ActionResult(success=True, message=f"单步执行：{intent.type.value}")


class AssistantWiringTests(unittest.TestCase):
    """确认 assistant 把多步任务交给 Agent、单步仍走老路径、挂起能被一句话接上。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        for name, fn in (
            ("_user_dir", lambda folder: self.root / folder),
            ("_journal_path", lambda: self.root / "journal.json"),
            ("_pending_path", lambda: self.root / "pending.json"),
            ("delete_path", lambda path: ActionResult(success=True, message=f"已放进回收站：{path}")),
        ):
            patcher = patch.object(files, name, side_effect=fn) if name == "_user_dir" \
                else patch.object(files, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.desktop / "报告.pdf").write_text("x", encoding="utf-8")
        (self.desktop / "旧图.png").write_bytes(b"x")

    def _assistant(self):
        from screen_agent.voice.assistant import VoiceAssistant

        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.wake_names = ["瑞塔"]
        assistant.session_enabled = True
        assistant.session_duration = 60
        assistant._in_session = False
        assistant._session_until = 0.0
        assistant._last_activity = 0.0
        assistant._on_status = None
        assistant._on_session = None
        assistant._on_options = None
        assistant._on_progress = None
        assistant._pending_options = None
        assistant.app_aliases = {}
        assistant.url_aliases = {}
        assistant.pipeline = None
        assistant.executor = _EchoExecutor()
        assistant.agent = build_agent(self.root)
        return assistant

    def test_multistep_goes_to_agent(self) -> None:
        assistant = self._assistant()
        result = assistant.handle_command("新建文件夹 素材 然后 把 桌面/报告.pdf 移动到 素材")
        self.assertIn("做完了", result.message)
        self.assertTrue((self.desktop / "素材" / "报告.pdf").exists())

    def test_single_step_stays_on_executor(self) -> None:
        assistant = self._assistant()
        result = assistant.handle_command("整理桌面文件")
        self.assertEqual(result.message, "单步执行：file_op")

    def test_suspend_then_resume_through_assistant(self) -> None:
        assistant = self._assistant()
        first = assistant.handle_command("删除 桌面/报告.pdf 然后 删除 桌面/旧图.png")
        self.assertIn("继续执行", first.options)

        second = assistant.handle_command("继续执行")  # 被 try_resume 接管
        self.assertTrue(assistant.agent.is_waiting)     # 第 2 步还要确认
        self.assertIn("继续执行", second.options)

        third = assistant.handle_command("取消任务")
        self.assertFalse(assistant.agent.is_waiting)
        self.assertIn("取消", third.message)

    def test_steering_falls_through_to_normal_routing(self) -> None:
        assistant = self._assistant()
        assistant.handle_command("删除 桌面/报告.pdf 然后 删除 桌面/旧图.png")
        result = assistant.handle_command("整理桌面文件")  # 不是回答 → 任务停掉，正常路由
        self.assertEqual(result.message, "单步执行：file_op")
        self.assertFalse(assistant.agent.is_waiting)


if __name__ == "__main__":
    unittest.main()
