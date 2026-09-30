"""飞轮难例队列的测试。

改这个的起因：原先 `_feed_flywheel` 用的是**丢弃式节流**——喂进一条之后，
30 秒内出现的其他难例**直接丢掉**，而且丢了多少完全看不到。
`data/eval/` 下至今没有 `flywheel.json`，说明飞轮压根没真正跑起来过。

改成有界队列后要保证三件事：
1. 样本先入队，不再当场丢
2. 队列满时丢**最旧**的，并把次数记在 `flywheel_dropped` 上（可观测）
3. 到点一次性排空写盘，单条失败不连坐
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.capture.watcher import DesktopWatcher, SightEvent
from screen_agent.eval.flywheel import Flywheel
from screen_agent.understand.classify import ActivityLabel


class FlywheelQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.flywheel = Flywheel(Path(self._tmp.name) / "flywheel.json")
        self.watcher = DesktopWatcher(
            flywheel=self.flywheel,
            use_uia=False,
            snapshot_windows=False,
            # 间隔设得极大：不自动排空，方便观察入队行为
            flywheel_interval=10_000.0,
        )

    def _event(self, text: str) -> SightEvent:
        return SightEvent(
            ts=datetime.now(), window_title="某窗口", process_name="x.exe", texts=[text]
        )

    def _label(self, activity: str = "other", confidence: float = 0.3,
               source: str = "rules") -> ActivityLabel:
        return ActivityLabel(activity=activity, confidence=confidence, source=source)

    def test_uncertain_sample_enters_queue_without_writing(self) -> None:
        """低置信度样本先入队，此时还不该落盘。"""
        for i in range(5):
            self.watcher._feed_flywheel(
                self._event(f"第{i}条界面文本内容"), self._label(), "other"
            )
        self.assertEqual(len(self.watcher._flywheel_queue), 5)
        self.assertEqual(self.flywheel.stats()["pending"], 0, "还在队列里就不该写盘")

    def test_drain_writes_everything_once(self) -> None:
        for i in range(5):
            self.watcher._feed_flywheel(
                self._event(f"第{i}条界面文本内容"), self._label(), "other"
            )
        written = self.watcher.drain_flywheel()
        self.assertEqual(written, 5)
        self.assertEqual(self.flywheel.stats()["pending"], 5)
        self.assertEqual(len(self.watcher._flywheel_queue), 0, "排空后队列应为空")

    def test_full_queue_drops_oldest_and_counts_it(self) -> None:
        """队列满时丢最旧的，且丢弃次数可观测——这是本次修复的核心。"""
        self.watcher._flywheel_queue = deque(maxlen=3)
        for i in range(5):
            self.watcher._feed_flywheel(
                self._event(f"第{i}条界面文本内容"), self._label(), "other"
            )
        self.assertEqual(len(self.watcher._flywheel_queue), 3)
        self.assertEqual(self.watcher.flywheel_dropped, 2, "丢了 2 条就该记 2")

    def test_confident_sample_is_not_queued(self) -> None:
        """有把握的判断不该占用队列——否则队列会被正常样本灌满。"""
        self.watcher._feed_flywheel(
            self._event("很明确的界面文本内容"), self._label(confidence=0.95), "other"
        )
        self.assertEqual(len(self.watcher._flywheel_queue), 0)

    def test_disagreement_has_priority_over_low_confidence(self) -> None:
        """规则与模型打架时走 disagreement 通道（哪怕置信度不低）。"""
        self.watcher._feed_flywheel(
            self._event("两路判定不一致的界面文本"),
            self._label(activity="coding", confidence=0.9, source="http"),
            "reading",
        )
        self.assertEqual(len(self.watcher._flywheel_queue), 1)
        kind, args = self.watcher._flywheel_queue[0]
        self.assertEqual(kind, "dis")
        self.assertEqual(args["rule_guess"], "reading")
        self.assertEqual(args["model_guess"], "coding")

    def test_drain_is_safe_when_no_flywheel(self) -> None:
        bare = DesktopWatcher(flywheel=None, use_uia=False, snapshot_windows=False)
        self.assertEqual(bare.drain_flywheel(), 0)

    def test_dropped_counter_starts_at_zero(self) -> None:
        self.assertEqual(self.watcher.flywheel_dropped, 0)


if __name__ == "__main__":
    unittest.main()
