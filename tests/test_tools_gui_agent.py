"""GUI agent loop 测试：mock 视觉模型与 SendInput，验证动作解析/执行/终止条件。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.tools.gui_agent import GuiAgent, _parse_action
from screen_agent.tools.registry_setup import build_default_registry


def _agent(max_steps: int = 5) -> GuiAgent:
    return GuiAgent(
        build_default_registry(),
        base_url="https://fake/v1", model="fake-vl", api_key="fake",
        max_steps=max_steps,
    )


class ParseActionTests(unittest.TestCase):
    def test_click(self) -> None:
        action = _parse_action('{"action": "click", "x": 100, "y": 200, "thought": "按钮"}')
        self.assertEqual((action.action, action.x, action.y), ("click", 100, 200))

    def test_code_block_stripped(self) -> None:
        action = _parse_action('```json\n{"action": "done", "thought": "ok"}\n```')
        self.assertEqual(action.action, "done")

    def test_invalid_rejected(self) -> None:
        self.assertIsNone(_parse_action("不是 JSON"))
        self.assertIsNone(_parse_action('{"action": "格式化磁盘"}'))


class LoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = _agent()
        self.send_input = MagicMock()
        p = patch("screen_agent.tools._win._send_input", self.send_input)
        p.start()
        self.addCleanup(p.stop)
        shot = patch.object(GuiAgent, "_screenshot_b64", lambda self: ("fakeb64", 1920, 1080))
        shot.start()
        self.addCleanup(shot.stop)

    def test_click_then_done(self) -> None:
        responses = [
            json.dumps({"action": "click", "x": 500, "y": 300, "thought": "确定按钮"}),
            json.dumps({"action": "done", "thought": "弹窗已关"}),
        ]
        with patch.object(GuiAgent, "_vision_complete", side_effect=responses):
            result = self.agent.run("点掉确定弹窗")
        self.assertTrue(result.success)
        self.assertGreaterEqual(self.send_input.call_count, 2)  # down+up

    def test_max_steps_stops(self) -> None:
        always = json.dumps({"action": "click", "x": 1, "y": 1, "thought": "继续点"})
        agent = _agent(max_steps=3)
        with patch.object(GuiAgent, "_vision_complete", return_value=always):
            result = agent.run("无限循环任务")
        self.assertFalse(result.success)
        self.assertIn("最大步数", result.message)

    def test_invalid_output_stops_without_executing(self) -> None:
        with patch.object(GuiAgent, "_vision_complete", return_value="模型胡言乱语"):
            result = self.agent.run("随便")
        self.assertFalse(result.success)
        self.assertIn("无法解析", result.message)
        self.assertEqual(self.send_input.call_count, 0)  # 不盲执行

    def test_fail_action(self) -> None:
        with patch.object(GuiAgent, "_vision_complete",
                          return_value=json.dumps({"action": "fail", "thought": "屏幕上没有目标"})):
            result = self.agent.run("点一个不存在的东西")
        self.assertFalse(result.success)
        self.assertIn("屏幕上没有目标", result.message)


class IntentWiringTests(unittest.TestCase):
    def test_gui_task_intent(self) -> None:
        from screen_agent.voice.intents import IntentType, parse_intent

        intent = parse_intent("帮我点一下确定按钮", {}, {})
        self.assertEqual(intent.type, IntentType.GUI_TASK)
        self.assertIn("确定按钮", intent.target)

    def test_chat_still_fallback(self) -> None:
        from screen_agent.voice.intents import IntentType, parse_intent

        intent = parse_intent("今天天气不错", {}, {})
        self.assertEqual(intent.type, IntentType.CHAT)


if __name__ == "__main__":
    unittest.main()
