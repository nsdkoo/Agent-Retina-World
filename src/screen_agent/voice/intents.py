from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class IntentType(str, Enum):
    SCREENSHOT = "screenshot"
    ANALYZE_SCREEN = "analyze_screen"
    OPEN_URL = "open_url"
    OPEN_APP = "open_app"
    DAILY_REPORT = "daily_report"
    TIMELINE = "timeline"
    STATS = "stats"
    OPEN_WEB_UI = "open_web_ui"
    END_SESSION = "end_session"
    CLOSE_APP = "close_app"
    VOLUME = "volume"
    CLIPBOARD = "clipboard"
    FIND_FILES = "find_files"
    LIST_WINDOWS = "list_windows"
    FOCUS_WINDOW = "focus_window"
    LOCK_SCREEN = "lock_screen"
    GUI_TASK = "gui_task"
    CHAT = "chat"
    UNKNOWN = "unknown"


@dataclass
class Intent:
    type: IntentType
    target: str = ""
    raw_command: str = ""
    tool: str = ""                       # 工具层路由名（如 app.close），非空时 executor 走 registry
    params: dict = field(default_factory=dict)


_URL_RE = re.compile(r"https?://[^\s]+", re.I)


def parse_intent(command: str, app_aliases: dict[str, str], url_aliases: dict[str, str]) -> Intent:
    text = command.strip()
    if not text:
        return Intent(IntentType.UNKNOWN, raw_command=text)

    if re.search(r"截图|截屏|截个图|截下图", text):
        return Intent(IntentType.SCREENSHOT, raw_command=text)

    if re.search(r"分析(?:一下)?屏幕|看看屏幕|理解(?:一下)?屏幕|我在干什么|看看我在|屏幕理解", text):
        return Intent(IntentType.ANALYZE_SCREEN, raw_command=text)

    # ---- 工具层意图（CLOSE_APP 需在 END_SESSION 前，避免"退出微信"被截胡）----
    close_m = re.search(r"(?:关闭|关掉|杀掉|退出)\s*([\u4e00-\u9fffA-Za-z][^\s]*)", text)
    if close_m and close_m.group(1) not in ("对话", "会话"):
        return Intent(
            IntentType.CLOSE_APP, target=close_m.group(1), raw_command=text,
            tool="app.close", params={"target": close_m.group(1)},
        )

    if re.search(r"日报|今日总结|每日总结|今天总结|总结一下", text):
        return Intent(IntentType.DAILY_REPORT, raw_command=text)

    if re.search(r"时间线|活动记录", text):
        return Intent(IntentType.TIMELINE, raw_command=text)

    if re.search(r"退出|没事了|再见|结束对话|退下吧|先这样", text):
        return Intent(IntentType.END_SESSION, raw_command=text)

    if re.search(r"统计|运行状态|状态", text):
        return Intent(IntentType.STATS, raw_command=text)

    if re.search(r"打开面板|打开网页面板|打开时间线网页|打开界面", text):
        return Intent(IntentType.OPEN_WEB_UI, raw_command=text)

    if re.search(r"音量(?:调)?(?:大|高)一?点?", text):
        return Intent(IntentType.VOLUME, raw_command=text, tool="volume.up", params={"steps": 3})
    if re.search(r"音量(?:调)?(?:小|低)一?点?", text):
        return Intent(IntentType.VOLUME, raw_command=text, tool="volume.down", params={"steps": 3})
    if re.search(r"(?:静音|取消静音|闭麦)", text):
        return Intent(IntentType.VOLUME, raw_command=text, tool="volume.mute")

    if re.search(r"剪贴板(?:里|中)?(?:有|是)?(?:什么|内容)", text):
        return Intent(IntentType.CLIPBOARD, raw_command=text, tool="clip.get")
    clip_set = re.search(r"(?:复制一下|复制到剪贴板|帮我复制)(?:这?段?)[:：]?(.+)", text)
    if clip_set:
        return Intent(
            IntentType.CLIPBOARD, target=clip_set.group(1).strip(), raw_command=text,
            tool="clip.set", params={"text": clip_set.group(1).strip()},
        )

    find_m = re.search(r"(?:找一下|找|搜索|查找)(?:文件|文档)\s*(.+)", text)
    if find_m:
        return Intent(
            IntentType.FIND_FILES, target=find_m.group(1).strip(), raw_command=text,
            tool="files.find", params={"pattern": find_m.group(1).strip()},
        )

    if re.search(r"(?:列出|看看|有哪些)(?:打开的)?窗口", text):
        return Intent(IntentType.LIST_WINDOWS, raw_command=text, tool="win.list")
    focus_m = re.search(r"(?:切换|切)(?:到|至)\s*(.+?)(?:的)?(?:窗口|界面)?$", text)
    if focus_m and focus_m.group(1).strip():
        return Intent(
            IntentType.FOCUS_WINDOW, target=focus_m.group(1).strip(), raw_command=text,
            tool="win.focus", params={"title": focus_m.group(1).strip()},
        )
    if re.search(r"(?:锁屏|锁定(?:电脑|屏幕))", text):
        return Intent(IntentType.LOCK_SCREEN, raw_command=text, tool="sys.lock")

    url_match = _URL_RE.search(text)
    if url_match:
        return Intent(IntentType.OPEN_URL, target=url_match.group(0), raw_command=text)

    open_web = re.search(r"打开网页\s*(.+)", text)
    if open_web:
        target = open_web.group(1).strip()
        if not target.startswith("http"):
            target = "https://" + target
        return Intent(IntentType.OPEN_URL, target=target, raw_command=text)

    open_m = re.search(r"打开\s*(.+)", text)
    if open_m:
        target = open_m.group(1).strip()
        for alias, url in url_aliases.items():
            if alias in target:
                return Intent(IntentType.OPEN_URL, target=url, raw_command=text)
        for alias, app in app_aliases.items():
            if alias.lower() in target.lower() or target.lower() in alias.lower():
                return Intent(IntentType.OPEN_APP, target=app, raw_command=text)
        if "." in target and " " not in target:
            return Intent(IntentType.OPEN_URL, target=f"https://{target}", raw_command=text)
        return Intent(IntentType.OPEN_APP, target=target, raw_command=text)

    gui_m = re.search(r"帮我(?:点|点击|输入|按|选)\s*(.+)", text)
    if gui_m:
        return Intent(IntentType.GUI_TASK, target=gui_m.group(1).strip(), raw_command=text)

    help_m = re.search(r"帮(?:我|忙)?(.+)", text)
    if help_m:
        inner = help_m.group(1).strip()
        return parse_intent(inner, app_aliases, url_aliases)

    return Intent(IntentType.CHAT, raw_command=text)
