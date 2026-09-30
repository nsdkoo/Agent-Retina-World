"""文件写工具测试：白名单 / 重名不覆盖 / 归档三阶段 / 撤销 / 意图路由。

全程在临时目录里跑（把用户目录与日志路径 patch 到 tmp），绝不碰真实桌面。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.tools import files
from screen_agent.voice.executor import CommandExecutor
from screen_agent.voice.intents import Intent, IntentType, parse_intent


class FilesWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.desktop = self.root / "Desktop"
        self.downloads = self.root / "Downloads"
        self.desktop.mkdir()
        self.downloads.mkdir()

        for name, fn in (
            ("_user_dir", lambda folder: self.root / folder),
            ("_journal_path", lambda: self.root / "journal.json"),
            ("_pending_path", lambda: self.root / "pending.json"),
        ):
            patcher = patch.object(files, name, side_effect=fn) if name == "_user_dir" \
                else patch.object(files, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- 白名单 ----

    def test_guard_allows_user_dir(self) -> None:
        self.assertIsNone(files._guard_write(self.desktop / "a.txt"))

    def test_guard_rejects_outside(self) -> None:
        reason = files._guard_write(self.root / "elsewhere" / "a.txt")
        self.assertIsNotNone(reason)

    def test_delete_outside_root_rejected(self) -> None:
        stray = self.root / "elsewhere.txt"
        stray.write_text("x", encoding="utf-8")
        result = files.delete_path(str(stray))
        self.assertFalse(result.success)
        self.assertTrue(stray.exists())

    # ---- 单个写操作 ----

    def test_mkdir_then_undo(self) -> None:
        result = files.make_dir("项目资料")
        self.assertTrue(result.success)
        created = self.desktop / "项目资料"
        self.assertTrue(created.is_dir())

        undo = files.undo_last()
        self.assertTrue(undo.success)
        self.assertFalse(created.exists())

    def test_move_never_overwrites(self) -> None:
        first = self.desktop / "a.txt"
        first.write_text("1", encoding="utf-8")
        self.assertTrue(files.move_path(str(first), str(self.downloads)).success)
        self.assertTrue((self.downloads / "a.txt").exists())

        second = self.desktop / "a.txt"
        second.write_text("2", encoding="utf-8")
        self.assertTrue(files.move_path(str(second), str(self.downloads)).success)
        self.assertTrue((self.downloads / "a_1.txt").exists())

    def test_move_undo_restores_position(self) -> None:
        src = self.desktop / "report.pdf"
        src.write_text("x", encoding="utf-8")
        files.move_path(str(src), "下载")
        self.assertTrue((self.downloads / "report.pdf").exists())

        self.assertTrue(files.undo_last().success)
        self.assertTrue(src.exists())
        self.assertFalse((self.downloads / "report.pdf").exists())

    def test_copy_keeps_source(self) -> None:
        src = self.desktop / "note.txt"
        src.write_text("x", encoding="utf-8")
        self.assertTrue(files.copy_path(str(src), "下载").success)
        self.assertTrue(src.exists())
        self.assertTrue((self.downloads / "note.txt").exists())

    def test_rename_keeps_suffix(self) -> None:
        src = self.desktop / "old.txt"
        src.write_text("x", encoding="utf-8")
        self.assertTrue(files.rename_path(str(src), "new").success)
        self.assertTrue((self.desktop / "new.txt").exists())

    def test_rename_refuses_existing_name(self) -> None:
        (self.desktop / "a.txt").write_text("a", encoding="utf-8")
        (self.desktop / "b.txt").write_text("b", encoding="utf-8")
        result = files.rename_path(str(self.desktop / "a.txt"), "b.txt")
        self.assertFalse(result.success)
        self.assertTrue((self.desktop / "a.txt").exists())

    # ---- 归档三阶段 ----

    def _seed_desktop(self) -> None:
        (self.desktop / "pic.png").write_bytes(b"x")
        (self.desktop / "doc.pdf").write_bytes(b"x")
        (self.desktop / "note.txt").write_text("x", encoding="utf-8")
        (self.desktop / "shortcut.lnk").write_text("x", encoding="utf-8")
        (self.desktop / "素材").mkdir()

    def test_organize_asks_first(self) -> None:
        self._seed_desktop()
        result = files.organize_dir(path="桌面")
        self.assertTrue(result.success)
        self.assertIsNotNone(result.options)
        self.assertIn("按类型归档", result.options)
        self.assertEqual(result.detail["count"], 3)  # .lnk 与子目录不计

    def test_organize_preview_then_apply(self) -> None:
        self._seed_desktop()
        files.organize_dir(path="桌面")

        preview = files.organize_dir(mode="type")
        self.assertIn("确认归档", preview.options)
        self.assertIn("图片", preview.message)
        self.assertFalse((self.desktop / "图片").exists())  # 预览阶段不许动

        done = files.organize_dir(mode="type", apply=True)
        self.assertTrue(done.success)
        self.assertTrue((self.desktop / "图片" / "pic.png").exists())
        self.assertTrue((self.desktop / "文档" / "doc.pdf").exists())
        self.assertTrue((self.desktop / "文档" / "note.txt").exists())
        self.assertTrue((self.desktop / "shortcut.lnk").exists())  # 快捷方式原地不动
        self.assertTrue((self.desktop / "素材").is_dir())          # 子目录不动

    def test_organize_undo(self) -> None:
        self._seed_desktop()
        files.organize_dir(path="桌面")
        files.organize_dir(mode="type")
        files.organize_dir(mode="type", apply=True)

        self.assertTrue(files.undo_last().success)
        self.assertTrue((self.desktop / "pic.png").exists())
        self.assertTrue((self.desktop / "doc.pdf").exists())
        self.assertFalse((self.desktop / "图片").exists())

    def test_organize_cancel(self) -> None:
        self._seed_desktop()
        files.organize_dir(path="桌面")
        cancelled = files.organize_dir(mode="cancel")
        self.assertIn("先不动", cancelled.message)
        self.assertFalse(files.organize_dir(mode="type", apply=True).success)

    def test_organize_without_pending_refuses_apply(self) -> None:
        self.assertFalse(files.organize_dir(apply=True).success)

    def test_organize_empty_dir(self) -> None:
        result = files.organize_dir(path="下载")
        self.assertTrue(result.success)
        self.assertIn("整齐", result.message)


class FileIntentTests(unittest.TestCase):
    APPS: dict[str, str] = {}
    URLS: dict[str, str] = {}

    def _intent(self, text: str):
        return parse_intent(text, self.APPS, self.URLS)

    def test_organize_desktop(self) -> None:
        intent = self._intent("整理桌面文件")
        self.assertEqual(intent.type, IntentType.FILE_OP)
        self.assertEqual(intent.tool, "files.organize")
        self.assertEqual(intent.params["path"], "桌面")

    def test_organize_flow_choices(self) -> None:
        self.assertEqual(self._intent("按类型归档").params, {"mode": "type"})
        self.assertEqual(self._intent("按修改时间归档").params, {"mode": "date"})
        self.assertEqual(self._intent("确认归档").params, {"apply": True})
        self.assertEqual(self._intent("算了先不动").params, {"mode": "cancel"})

    def test_mkdir(self) -> None:
        intent = self._intent("新建文件夹 项目资料")
        self.assertEqual(intent.tool, "files.mkdir")
        self.assertEqual(intent.params["path"], "项目资料")

    def test_move_copy_rename_delete(self) -> None:
        self.assertEqual(self._intent("把 a.pdf 移动到 下载").params,
                         {"src": "a.pdf", "dst": "下载"})
        self.assertEqual(self._intent("把 a.pdf 复制到 文档").params,
                         {"src": "a.pdf", "dst": "文档"})
        self.assertEqual(self._intent("把 a.txt 改名为 b.txt").params,
                         {"src": "a.txt", "new_name": "b.txt"})
        self.assertEqual(self._intent("删除 临时.txt").params, {"path": "临时.txt"})

    def test_undo(self) -> None:
        self.assertEqual(self._intent("撤销").tool, "files.undo")

    def test_chitchat_not_hijacked(self) -> None:
        self.assertEqual(self._intent("你好啊").type, IntentType.CHAT)
        self.assertEqual(self._intent("整理一下简历").type, IntentType.CHAT)

    def test_open_with_trailing_clause_is_trimmed(self) -> None:
        intent = self._intent("打开qq给我的小号 发个消息")
        self.assertEqual(intent.type, IntentType.OPEN_APP)
        self.assertEqual(intent.target, "qq")


class ChatToolLoopTests(unittest.TestCase):
    """对话轮必须能拿到工具列表——registry 惰性初始化曾把工具轮整段吞掉，
    于是模型只能回「正在为您整理桌面文件，请稍等」这种空话。"""

    class _FakeChat:
        def __init__(self) -> None:
            self.tool_rounds = 0

        def complete_with_tools(self, messages, system=None, tools=None):  # noqa: ANN001
            self.tool_rounds += 1
            self.last_tools = tools
            if self.tool_rounds == 1:
                return {
                    "tool_calls": [{
                        "name": "files.organize",
                        "arguments": '{"path": "桌面"}',
                        "id": "call_1",
                    }],
                    "content": "",
                    "raw_tool_calls": [],
                }
            return {"tool_calls": [], "content": "列好了", "raw_tool_calls": []}

        def complete_stream(self, messages, system=None, on_delta=None):  # noqa: ANN001
            return "列好了"

    def test_tool_round_enabled_on_first_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = root / "Desktop"
            desktop.mkdir()
            (desktop / "a.png").write_bytes(b"x")
            with patch.object(files, "_user_dir", side_effect=lambda n: root / n), \
                 patch.object(files, "_journal_path", lambda: root / "j.json"), \
                 patch.object(files, "_pending_path", lambda: root / "p.json"):
                fake = self._FakeChat()
                executor = CommandExecutor(None, chat_client=fake)  # type: ignore[arg-type]
                result = executor.run(Intent(IntentType.CHAT, raw_command="把桌面收拾一下"))

        self.assertEqual(fake.tool_rounds, 1)
        self.assertTrue(fake.last_tools)                       # 工具列表真的喂给模型了
        self.assertIsNotNone(result.options)                   # 需要拍板 → 选项上浮
        self.assertIn("按类型归档", result.options)
        self.assertTrue(result.detail.get("ask"))


if __name__ == "__main__":
    unittest.main()
