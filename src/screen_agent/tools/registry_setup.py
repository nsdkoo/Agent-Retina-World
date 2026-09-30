"""默认工具注册表：把全部能力模块装配成 ToolRegistry。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from screen_agent.tools import clipboard, dialog, files, input, shell, system, volume, windows
from screen_agent.tools.apps import AppResolver
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec
from screen_agent.voice.executor import ActionResult


def build_default_registry(
    app_resolver: AppResolver | None = None,
    confirm_fn: Callable[[ToolSpec, dict], bool] | None = None,
    extra_roots: list[str] | None = None,
    journal=None,  # noqa: ANN001 - MemoryStore 的 DesktopJournal，注入式避免工具层依赖记忆层
) -> ToolRegistry:
    resolver = app_resolver or AppResolver(
        Path(__file__).resolve().parents[3] / "data" / "cache" / "app_index.json"
    )
    if extra_roots:
        # 白名单之外额外放开的可写根目录（配置里显式指定；沙箱实测也从这里走）
        files.set_extra_roots(extra_roots)
    registry = ToolRegistry(confirm_fn=confirm_fn)
    register = registry.register

    # ---- 应用 ----
    def _app_open(target: str) -> ActionResult:
        resolved = resolver.resolve(target)
        if resolved is None:
            return ActionResult(success=False, message=f"没找到「{target}」这个应用")
        resolver.launch(resolved)
        return ActionResult(success=True, message=f"正在打开 {target}", detail={"app": resolved.target})

    def _app_close(target: str) -> ActionResult:
        return windows.close_app_safe(target)

    def _app_list() -> ActionResult:
        names = sorted({lnk.stem for lnk in resolver._collect_lnk_files()})
        preview = "、".join(names[:40])
        return ActionResult(success=True, message=f"开始菜单应用（前 40）：{preview}", detail={"count": len(names)})

    register(ToolSpec("app.open", "打开应用", _app_open, RiskLevel.LOW, {"target": "应用名或路径"},
                      schema={"target": {"type": "string", "description": "应用名或路径",
                                         "required": True}}))
    register(ToolSpec("app.close", "关闭应用", _app_close, RiskLevel.HIGH, {"target": "进程名"},
                      schema={"target": {"type": "string", "description": "进程名",
                                         "required": True}}))
    register(ToolSpec("app.list", "列出已装应用", _app_list, RiskLevel.SAFE, idempotent=True))

    # ---- 文件（读）----
    # 纯读工具标 idempotent=True：失败重试不会产生副作用，是错误恢复里唯一敢自动重试的一类
    register(ToolSpec("files.open", "打开文件或文件夹", files.open_path, RiskLevel.LOW, {"path": "路径"},
                      schema={"path": {"type": "string", "description": "路径", "required": True}}))
    register(ToolSpec("files.list", "列出目录内容", files.list_dir, RiskLevel.SAFE, {"path": "目录"},
                      schema={"path": {"type": "string", "description": "目录，默认桌面"}},
                      idempotent=True))
    register(ToolSpec("files.find", "模糊找文件", files.find_files, RiskLevel.SAFE,
                      {"pattern": "文件名关键词"},
                      schema={"pattern": {"type": "string", "description": "文件名关键词",
                                          "required": True}},
                      idempotent=True))

    # ---- 文件（写）——白名单 + 重名不覆盖 + 操作日志，delete 走回收站且需确认 ----
    # 写工具**一律不标 idempotent**（默认 False）：move 重跑会再移一次、
    # mkdir 重跑会报已存在——宁可少救，不可重试出副作用
    register(ToolSpec("files.mkdir", "新建文件夹", files.make_dir, RiskLevel.LOW,
                      {"path": "文件夹名或路径"},
                      schema={"path": {"type": "string", "description": "文件夹名或路径",
                                       "required": True}}))
    register(ToolSpec("files.move", "移动文件到目标目录", files.move_path, RiskLevel.LOW,
                      {"src": "源路径", "dst": "目标目录或完整路径"},
                      schema={"src": {"type": "string", "description": "源路径", "required": True},
                              "dst": {"type": "string", "description": "目标目录或完整路径",
                                      "required": True}}))
    register(ToolSpec("files.copy", "复制文件到目标目录", files.copy_path, RiskLevel.LOW,
                      {"src": "源路径", "dst": "目标目录或完整路径"},
                      schema={"src": {"type": "string", "description": "源路径", "required": True},
                              "dst": {"type": "string", "description": "目标目录或完整路径",
                                      "required": True}}))
    register(ToolSpec("files.rename", "重命名文件", files.rename_path, RiskLevel.LOW,
                      {"src": "源路径", "new_name": "新名字"},
                      schema={"src": {"type": "string", "description": "源路径", "required": True},
                              "new_name": {"type": "string", "description": "新名字",
                                           "required": True}}))
    register(ToolSpec("files.organize", "把目录里的文件按类型或修改时间归档进子文件夹（先出清单再动手）",
                      files.organize_dir, RiskLevel.LOW,
                      {"path": "目录，默认桌面", "mode": "type 按类型 / date 按时间 / 留空则先问用户"},
                      schema={"path": {"type": "string", "description": "目录，默认桌面"},
                              "mode": {"type": "string", "description": "type 按类型 / date 按时间"}}))
    register(ToolSpec("files.delete", "删除文件（放进回收站，可还原）", files.delete_path,
                      RiskLevel.HIGH, {"path": "路径"},
                      schema={"path": {"type": "string", "description": "路径", "required": True}}))
    register(ToolSpec("files.undo", "撤销上一次文件操作", files.undo_last, RiskLevel.LOW))

    # ---- 文件内容层：读 / 写 / 改 / 搜 / 匹配（对标 Codex · WorkBuddy 的 read/write/edit/grep/glob）----
    register(ToolSpec("files.read", "读文件内容，返回带行号的文本", files.read_file, RiskLevel.SAFE,
                      {"path": "文件路径", "offset": "从第几行开始（可留空）",
                       "max_lines": "最多读多少行（可留空）"},
                      schema={"path": {"type": "string", "description": "文件路径", "required": True},
                              "offset": {"type": "integer", "description": "从第几行开始"},
                              "max_lines": {"type": "integer", "description": "最多读多少行"}},
                      idempotent=True))
    register(ToolSpec("files.write", "把内容写进文件，覆盖前自动备份、可撤销",
                      files.write_file, RiskLevel.LOW,
                      {"path": "文件路径", "content": "要写入的内容",
                       "mode": "overwrite 覆盖 / append 追加"},
                      schema={"path": {"type": "string", "description": "文件路径", "required": True},
                              "content": {"type": "string", "description": "要写入的内容",
                                          "required": True},
                              "mode": {"type": "string", "description": "overwrite 覆盖 / append 追加"}}))
    register(ToolSpec("files.edit", "精确替换文件里的一处内容（匹配到多处或零处都拒绝执行）",
                      files.edit_file, RiskLevel.LOW,
                      {"path": "文件路径", "old_text": "被替换的内容", "new_text": "替换成什么"},
                      schema={"path": {"type": "string", "description": "文件路径", "required": True},
                              "old_text": {"type": "string", "description": "被替换的内容",
                                           "required": True},
                              "new_text": {"type": "string", "description": "替换成什么",
                                           "required": True}}))
    register(ToolSpec("files.grep", "按内容搜索文件，返回「文件:行号: 内容」",
                      files.grep_files, RiskLevel.SAFE,
                      {"pattern": "要搜的内容或正则", "path": "在哪个目录搜（默认桌面）",
                       "suffix": "只搜某类文件，如 .py（可留空）"},
                      schema={"pattern": {"type": "string", "description": "要搜的内容或正则",
                                          "required": True},
                              "path": {"type": "string", "description": "在哪个目录搜，默认桌面"},
                              "suffix": {"type": "string", "description": "只搜某类文件，如 .py"}},
                      idempotent=True))
    register(ToolSpec("files.glob", "按通配符列路径，如 *.py、**/*.md",
                      files.glob_files, RiskLevel.SAFE,
                      {"pattern": "匹配式", "path": "在哪个目录找（默认桌面）"},
                      schema={"pattern": {"type": "string", "description": "匹配式", "required": True},
                              "path": {"type": "string", "description": "在哪个目录找，默认桌面"}},
                      idempotent=True))

    # ---- 命令执行（对标 Codex 的 exec_command：只读放行 / 破坏性要确认 / 致命直接拒）----
    register(ToolSpec("shell.run", "在受限工作目录里执行一条命令并返回输出",
                      shell.run_command, RiskLevel.HIGH,
                      {"cmd": "要执行的命令", "workdir": "工作目录（默认桌面）",
                       "timeout": "超时秒数，默认 30", "max_output": "输出字符上限"},
                      schema={"cmd": {"type": "string", "description": "要执行的命令", "required": True},
                              "workdir": {"type": "string", "description": "工作目录，默认桌面"},
                              "timeout": {"type": "number", "description": "超时秒数，默认 30"},
                              "max_output": {"type": "integer", "description": "输出字符上限"}}))

    # ---- 桌面行为记忆（注入式：没接记忆库时这两个工具不注册）----
    if journal is not None:
        def _journal_today() -> ActionResult:
            summary = journal.day_summary()
            if not summary["total"]:
                return ActionResult(success=True, message="今天还没记录到什么活动")
            lines = [f"今天（{summary['date']}）记了 {summary['total']} 条，{summary['first']}—{summary['last']}"]
            for app, seconds in summary["dwell"][:6]:
                lines.append(f"  · {app}：约 {seconds // 60} 分钟")
            others = [f"{a}({c}次)" for a, c in summary["apps"][:8]]
            lines.append("应用出现次数：" + "、".join(others))
            return ActionResult(success=True, message="\n".join(lines), detail=summary)

        def _journal_search(query: str, app: str = "") -> ActionResult:
            rows = journal.search(query, app=app, limit=10)
            if not rows:
                return ActionResult(success=False, message=f"活动记录里没搜到「{query}」")
            lines = []
            for row in rows:
                when = row["ts"][5:16].replace("T", " ")
                body = (row["digest"] or row["window_title"] or "")[:60]
                lines.append(f"- {when} {row['app']}：{body}")
            return ActionResult(
                success=True,
                message=f"找到 {len(rows)} 条与「{query}」相关的记录：\n" + "\n".join(lines),
                detail={"rows": rows},
            )

        register(ToolSpec("journal.today", "今天在电脑上做了什么（应用与停留时长）",
                          _journal_today, RiskLevel.SAFE, idempotent=True))
        register(ToolSpec("journal.search", "在桌面活动记录里按内容搜索",
                          _journal_search, RiskLevel.SAFE,
                          {"query": "要搜的内容", "app": "限定应用（可留空）"},
                          schema={"query": {"type": "string", "description": "要搜的内容",
                                            "required": True},
                                  "app": {"type": "string", "description": "限定应用，可留空"}},
                          idempotent=True))

    # ---- 交互：把需要用户拍板的事变成可点选项 ----
    register(ToolSpec("ask.options", "需要用户拍板时提问并给出候选，别自己替用户猜",
                      dialog.ask_options, RiskLevel.SAFE,
                      {"question": "要问用户的问题", "options": "候选，用 | 分隔，最多 4 个"}))

    # ---- 剪贴板 ----
    register(ToolSpec("clip.get", "读剪贴板", lambda: clipboard.clip_get_text(), RiskLevel.SAFE))
    register(ToolSpec("clip.set", "写剪贴板", clipboard.clip_set_text, RiskLevel.LOW, {"text": "内容"}))

    # ---- 窗口 ----
    register(ToolSpec("win.list", "列出窗口", lambda: windows.list_windows(), RiskLevel.SAFE))
    register(ToolSpec("win.focus", "聚焦窗口", windows.focus_window, RiskLevel.LOW, {"title": "标题关键词"}))

    # ---- 输入（GUI 操控原语，HIGH）----
    register(ToolSpec("input.mouse_move", "移动鼠标", input.mouse_move, RiskLevel.HIGH, {"x": "横坐标", "y": "纵坐标"}))
    register(ToolSpec("input.mouse_click", "点击鼠标", input.mouse_click, RiskLevel.HIGH,
                      {"x": "横坐标", "y": "纵坐标", "button": "left/right/middle", "double": "是否双击"}))
    register(ToolSpec("input.type_text", "键入文字", input.type_text, RiskLevel.HIGH, {"text": "内容"}))
    register(ToolSpec("input.press_key", "按键", input.press_key, RiskLevel.HIGH, {"key": "键名 enter/esc/tab/字母"}))
    register(ToolSpec("input.scroll", "滚轮", input.scroll, RiskLevel.HIGH, {"delta": "格数，正上负下"}))

    # ---- 音量 ----
    register(ToolSpec("volume.up", "音量调大", volume.volume_up, RiskLevel.LOW, {"steps": "步数"}))
    register(ToolSpec("volume.down", "音量调小", volume.volume_down, RiskLevel.LOW, {"steps": "步数"}))
    register(ToolSpec("volume.mute", "静音切换", lambda: volume.volume_mute(), RiskLevel.LOW))
    register(ToolSpec("volume.get", "读音量", lambda: volume.volume_get(), RiskLevel.SAFE))

    # ---- 系统 ----
    register(ToolSpec("sys.lock", "锁屏", lambda: system.lock_screen(), RiskLevel.HIGH))
    register(ToolSpec("sys.screenshot_clip", "截屏到剪贴板", lambda: system.screenshot_to_clipboard(), RiskLevel.LOW))
    register(ToolSpec("sys.open_url", "打开网页", system.open_url, RiskLevel.LOW, {"url": "网址"}))

    return registry
