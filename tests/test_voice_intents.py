import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.understand.chat import DisabledChatClient, OpenAICompatibleChatClient, resolve_chat_api_key
from screen_agent.voice.assistant import VoiceAssistant
from screen_agent.voice.executor import ActionResult, CommandExecutor
from screen_agent.voice.intents import IntentType, parse_intent


class VoiceIntentTests(unittest.TestCase):
    APPS = {"微信": "WeChat", "Cursor": "Cursor"}
    URLS = {"百度": "https://www.baidu.com"}

    def test_screenshot(self) -> None:
        intent = parse_intent("帮我截个图", self.APPS, self.URLS)
        self.assertEqual(intent.type, IntentType.SCREENSHOT)

    def test_open_url_alias(self) -> None:
        intent = parse_intent("打开百度", self.APPS, self.URLS)
        self.assertEqual(intent.type, IntentType.OPEN_URL)
        self.assertIn("baidu", intent.target)

    def test_analyze(self) -> None:
        intent = parse_intent("看看我在干什么", self.APPS, self.URLS)
        self.assertEqual(intent.type, IntentType.ANALYZE_SCREEN)

    def test_end_session(self) -> None:
        intent = parse_intent("没事了", self.APPS, self.URLS)
        self.assertEqual(intent.type, IntentType.END_SESSION)

    def test_chat_fallback(self) -> None:
        intent = parse_intent("你好啊", self.APPS, self.URLS)
        self.assertEqual(intent.type, IntentType.CHAT)
        self.assertEqual(intent.raw_command, "你好啊")

    def test_command_over_chat(self) -> None:
        intent = parse_intent("你好，帮我截个图", self.APPS, self.URLS)
        self.assertEqual(intent.type, IntentType.SCREENSHOT)

    def test_wake_strip(self) -> None:
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.wake_names = ["小光", "Retina"]
        assistant.session_enabled = True
        assistant.session_duration = 60
        assistant._in_session = False
        assistant._session_until = 0.0
        assistant.app_aliases = self.APPS
        assistant.url_aliases = self.URLS
        assistant.pipeline = None  # type: ignore
        assistant.executor = None  # type: ignore

        self.assertTrue(assistant._contains_wake_word("小光，截图"))
        cmd = assistant._strip_wake_word("小光，帮我打开百度")
        self.assertIn("打开", cmd)


class SessionModeTests(unittest.TestCase):
    def test_session_command_without_wake(self) -> None:
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.wake_names = ["小光"]
        assistant.session_enabled = True
        assistant.session_duration = 60
        assistant._in_session = True
        assistant._session_until = 9999999999.0
        assistant.app_aliases = {}
        assistant.url_aliases = {}
        assistant._set_session = lambda *a, **k: None  # type: ignore
        assistant._extend_session = lambda: None  # type: ignore

        class FakeExecutor:
            def run(self, intent):
                return ActionResult(success=True, message="ok")

        assistant.executor = FakeExecutor()  # type: ignore
        result = assistant.handle_transcript("截图")
        self.assertIsNotNone(result)
        self.assertEqual(result.message, "ok")


class ChatExecutorTests(unittest.TestCase):
    def test_chat_disabled(self) -> None:
        pipeline = MagicMock()
        executor = CommandExecutor(pipeline, chat_client=DisabledChatClient())
        intent = parse_intent("你好", {}, {})
        result = executor.run(intent)
        self.assertFalse(result.success)
        self.assertIn("未配置", result.message)

    def test_chat_success(self) -> None:
        pipeline = MagicMock()
        pipeline.proactive.timeline_markdown.return_value = "- 浏览网页"
        client = MagicMock(spec=OpenAICompatibleChatClient)
        client.complete.return_value = "你好，我是助手"

        history: list[dict[str, str]] = []
        executor = CommandExecutor(
            pipeline,
            chat_client=client,
            chat_history=history,
            max_history=6,
            screen_context_fn=lambda: "最近在看文档",
        )
        intent = parse_intent("你好", {}, {})
        result = executor.run(intent)

        self.assertTrue(result.success)
        self.assertEqual(result.message, "你好，我是助手")
        self.assertEqual(len(history), 2)
        client.complete.assert_called_once()


class ChatKeyTests(unittest.TestCase):
    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False)
    def test_resolve_from_env(self) -> None:
        key = resolve_chat_api_key("", "OPENAI_API_KEY")
        self.assertEqual(key, "sk-test-key")


if __name__ == "__main__":
    unittest.main()
