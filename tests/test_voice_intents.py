import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.voice.assistant import VoiceAssistant
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

    def test_wake_strip(self) -> None:
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.wake_names = ["小光", "Retina"]
        self.assertTrue(assistant._contains_wake_word("小光，截图"))
        cmd = assistant._strip_wake_word("小光，帮我打开百度")
        self.assertIn("打开", cmd)


if __name__ == "__main__":
    unittest.main()
