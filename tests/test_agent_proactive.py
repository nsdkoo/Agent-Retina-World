"""主动准备的测试（差异化第三步：主动）。

改这个的起因：`proactive/` 原来只有三个**报告生成**方法（日报/待办/时间线），
本质是「你问它才答」。真正的主动服务是另一回事。

## 主动 ≠ 打扰

这是整套机制最容易被做坏的地方。多数「主动式助手」定时跳出来说点什么，
结果用户第一件事就是关掉通知。

真正的主动要同时满足三条，**少任何一条都会变成噪声**：

1. **知道有什么事值得做** —— 你交代过但没做的 / 上次没做完的 / 你的习惯
2. **知道现在是不是时候** —— 用户忙就别插嘴
3. **说得出具体做什么** —— 不是「要不要帮你整理」，而是「上次那个没做完，接着做吗」

第 3 条最容易忽略：含糊的建议等于把决策成本又推回给用户，那还不如不说。

所以这组测试盯的就是这三条。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.memory.store import MemoryStoreV2
from screen_agent.proactive.prepare import PreparationService, should_speak_now


class SpeakTimingTests(unittest.TestCase):
    """开口时机 —— 这套机制里最该守的地方。

    它做坏了**不会报错**，只会让用户默默把通知关掉，而你还以为一切正常。
    所以抽成纯函数直接测。
    """

    def test_busy_blocks(self) -> None:
        self.assertFalse(should_speak_now(
            interruptible=False, in_session=False,
            seconds_since_last=9999, min_interval=60,
        ))

    def test_in_session_blocks(self) -> None:
        """正在对话时插嘴会打断思路。"""
        self.assertFalse(should_speak_now(
            interruptible=True, in_session=True,
            seconds_since_last=9999, min_interval=60,
        ))

    def test_too_soon_blocks(self) -> None:
        """提得太勤等于骚扰。"""
        self.assertFalse(should_speak_now(
            interruptible=True, in_session=False,
            seconds_since_last=10, min_interval=600,
        ))

    def test_all_clear_opens(self) -> None:
        self.assertTrue(should_speak_now(
            interruptible=True, in_session=False,
            seconds_since_last=601, min_interval=600,
        ))

    def test_exactly_at_interval_opens(self) -> None:
        """边界取等号——正好到了就该放行，别因为差 0.001 秒一直不提。"""
        self.assertTrue(should_speak_now(
            interruptible=True, in_session=False,
            seconds_since_last=600, min_interval=600,
        ))

    def test_first_time_never_blocks_by_interval(self) -> None:
        """首次运行（上次时刻为 0）不该被间隔卡住。"""
        self.assertTrue(should_speak_now(
            interruptible=True, in_session=False,
            seconds_since_last=float("inf"), min_interval=600,
        ))


class InterruptionGateTests(unittest.TestCase):
    """时机门控 —— 这套机制的开关。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MemoryStoreV2(Path(self._tmp.name) / "m.db")

    def test_busy_means_silence(self) -> None:
        """用户正忙 → 一条建议都不提。

        这是**最该守住的**一条：主动机制做坏了不会报错，
        只会让用户默默把它关掉。
        """
        self.memory.add_intention("整理下载目录", due_at=datetime.fromisoformat("2026-09-30T10:00"))
        service = PreparationService(memory=self.memory)
        self.assertEqual(service.suggest(interruptible=False), [])

    def test_focused_means_silence(self) -> None:
        service = PreparationService(memory=self.memory)
        self.assertEqual(service.suggest(interruptible=False, now=datetime(2026, 9, 30, 15, 0)), [])


class IntentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MemoryStoreV2(Path(self._tmp.name) / "m.db")

    def test_pending_intention_surfaces(self) -> None:
        """用户明确交代过的事——确定性最高，最该提。"""
        self.memory.add_intention("给张老师回邮件", due_at=datetime.fromisoformat("2026-09-30T18:00"))
        service = PreparationService(memory=self.memory)
        found = service.suggest(now=datetime(2026, 9, 30, 15, 0))
        self.assertTrue(found)
        self.assertEqual(found[0].kind, "intention")
        self.assertIn("张老师", found[0].what)

    def test_overdue_gets_higher_confidence(self) -> None:
        """已过期的比还没到点儿的更该提。"""
        self.memory.add_intention("交周报", due_at=datetime.fromisoformat("2026-09-30T09:00"))
        service = PreparationService(memory=self.memory)
        found = service.suggest(now=datetime(2026, 9, 30, 15, 0))
        self.assertGreater(found[0].confidence, 0.8)
        self.assertIn("过了", found[0].why)


class InterruptedTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _service_with_trajectory(self, rows: list[dict]):  # noqa: ANN202
        class _FakeTraj:
            def recent(self, limit: int = 10):  # noqa: ANN202, ARG002
                return rows

        return PreparationService(trajectory=_FakeTraj())

    def test_interrupted_task_suggests_resume(self) -> None:
        """「接着做」比「从零开始」省事——这是主动最实在的价值。"""
        service = self._service_with_trajectory([
            {"task_id": "t-1", "goal": "整理下载目录", "state": "paused"},
        ])
        found = service.suggest()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "resume")
        self.assertIn("整理下载目录", found[0].what)
        self.assertIn("resume_from:t-1", found[0].steps)

    def test_finished_task_is_not_suggested(self) -> None:
        """做完的事不该再提——那是纯噪声。"""
        service = self._service_with_trajectory([
            {"task_id": "t-1", "goal": "整理下载目录", "state": "finished"},
        ])
        self.assertEqual(service.suggest(), [])

    def test_suggestion_carries_resume_token(self) -> None:
        """建议里要带上**可执行的钩子**（task_id），而不是只有一句空话。"""
        service = self._service_with_trajectory([
            {"task_id": "abc-123", "goal": "跑评测", "state": "failed"},
        ])
        found = service.suggest()
        self.assertEqual(found[0].steps, ["resume_from:abc-123"])


class HabitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MemoryStoreV2(Path(self._tmp.name) / "m.db")

    def _seed(self, resources: list[str], times: int) -> None:
        for i in range(times):
            self.memory.save_agent_episode(
                goal="整理目录", summary="整理（用了 files.move）",
                task_id=f"t-{i}", resources=resources,
            )

    def test_repeated_resource_becomes_habit(self) -> None:
        """动过多次的路径才算习惯——这正是 episode 要记 resources 的原因。"""
        self._seed([r"D:\下载"], times=4)
        service = PreparationService(memory=self.memory)
        found = service.suggest()
        self.assertTrue(any(s.kind == "habit" for s in found))
        habit = next(s for s in found if s.kind == "habit")
        self.assertIn(r"D:\下载", habit.what)

    def test_below_threshold_not_a_habit(self) -> None:
        """只出现过一两次的动作是偶发，不是习惯——当规律推会显得很蠢。"""
        self._seed([r"D:\偶尔"], times=1)
        service = PreparationService(memory=self.memory)
        found = service.suggest()
        self.assertFalse(any(s.kind == "habit" for s in found))

    def test_task_id_not_mistaken_for_resource(self) -> None:
        """`evidence_paths` 第一个是 task_id，不是「动过的路径」，别统计进去。"""
        # 注意：event_id 是 `agent-{task_id}`，同一个 task_id 重复写会**覆盖**而不是累加，
        # 所以这里必须用不同的 id 才攒得出「多次」这个条件
        for i in range(5):
            self.memory.save_agent_episode(
                goal="干活", summary="干活", task_id=f"tid-{i}", resources=[r"D:\真目录"],
            )
        service = PreparationService(memory=self.memory)
        found = [s for s in service.suggest() if s.kind == "habit"]
        self.assertTrue(found)
        self.assertIn(r"D:\真目录", found[0].what)


class OutputQualityTests(unittest.TestCase):
    """建议本身的表达质量——含糊的建议还不如不提。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MemoryStoreV2(Path(self._tmp.name) / "m.db")

    def test_max_suggestions_capped(self) -> None:
        """一次别抛太多——信息过载等于没有信息。"""
        for i in range(10):
            self.memory.add_intention(f"事情 {i}", due_at=datetime.fromisoformat("2026-09-30T09:00"))
        service = PreparationService(memory=self.memory, max_suggestions=3)
        self.assertLessEqual(len(service.suggest(now=datetime(2026, 9, 30, 15, 0))), 3)

    def test_sorted_by_confidence(self) -> None:
        """更确定的排前面——用户只读第一条也不亏。"""
        self.memory.add_intention("明确交代的事", due_at=datetime.fromisoformat("2026-09-30T09:00"))
        for i in range(4):
            self.memory.save_agent_episode(
                goal="干活", summary="干活", task_id=f"t-{i}", resources=[r"D:\常用"],
            )
        service = PreparationService(memory=self.memory)
        found = service.suggest(now=datetime(2026, 9, 30, 15, 0))
        scores = [s.confidence for s in found]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_render_is_readable(self) -> None:
        self.memory.add_intention("交周报", due_at=datetime.fromisoformat("2026-09-30T09:00"))
        service = PreparationService(memory=self.memory)
        text = service.suggest(now=datetime(2026, 9, 30, 15, 0))[0].render()
        self.assertIn("交周报", text)
        self.assertIn("（", text)

    def test_empty_sources_return_empty(self) -> None:
        """什么都没有时安静返回空表，别硬凑建议。"""
        service = PreparationService(memory=self.memory)
        self.assertEqual(service.suggest(now=datetime(2026, 9, 30, 15, 0)), [])

    def test_broken_source_does_not_crash(self) -> None:
        """一路数据源坏了不能把整体拖垮。"""
        class _Boom:
            def list_intentions(self, **kw):  # noqa: ANN003, ARG002
                raise RuntimeError("记忆库炸了")

            def list_events(self, limit=100):  # noqa: ANN001, ARG002
                raise RuntimeError("记忆库炸了")

        service = PreparationService(memory=_Boom())
        self.assertEqual(service.suggest(), [])


if __name__ == "__main__":
    unittest.main()
