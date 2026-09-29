"""P2 意图扩展测试：工具路由意图 + close_app 别名。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.voice.intents import IntentType, parse_intent


def _parse(text: str):
    return parse_intent(text, {"cursor": "Cursor"}, {"百度": "https://www.baidu.com"})


class ToolIntentTests(unittest.TestCase):
    def test_close_app_beats_end_session(self) -> None:
        intent = _parse("退出微信")
        self.assertEqual(intent.type, IntentType.CLOSE_APP)
        self.assertEqual(intent.params["target"], "微信")

    def test_bare_exit_still_end_session(self) -> None:
        self.assertEqual(_parse("退出").type, IntentType.END_SESSION)

    def test_volume_up(self) -> None:
        intent = _parse("音量调大一点")
        self.assertEqual(intent.tool, "volume.up")

    def test_mute(self) -> None:
        self.assertEqual(_parse("静音").tool, "volume.mute")

    def test_clipboard_get_and_set(self) -> None:
        self.assertEqual(_parse("剪贴板里有什么").tool, "clip.get")
        intent = _parse("复制一下hello world")
        self.assertEqual(intent.tool, "clip.set")
        self.assertEqual(intent.params["text"], "hello world")

    def test_find_files(self) -> None:
        intent = _parse("找文件周报")
        self.assertEqual(intent.tool, "files.find")
        self.assertEqual(intent.params["pattern"], "周报")

    def test_list_and_focus_window(self) -> None:
        self.assertEqual(_parse("列出打开的窗口").tool, "win.list")
        intent = _parse("切到微信窗口")
        self.assertEqual(intent.tool, "win.focus")
        self.assertEqual(intent.params["title"], "微信")

    def test_lock_screen(self) -> None:
        self.assertEqual(_parse("锁屏").tool, "sys.lock")

    def test_open_still_works(self) -> None:
        intent = _parse("打开qq")
        self.assertEqual(intent.type, IntentType.OPEN_APP)
        self.assertEqual(intent.target, "qq")

    def test_chat_fallback_preserved(self) -> None:
        self.assertEqual(_parse("今天心情不错").type, IntentType.CHAT)


class CloseAppAliasTests(unittest.TestCase):
    def test_wechat_maps_to_exe(self) -> None:
        from screen_agent.tools.windows import _COMMON_EXE

        self.assertEqual(_COMMON_EXE["微信"], "WeChat")


if __name__ == "__main__":
    unittest.main()
