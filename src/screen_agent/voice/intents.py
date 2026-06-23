from __future__ import annotations

import re
from dataclasses import dataclass
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
    UNKNOWN = "unknown"


@dataclass
class Intent:
    type: IntentType
    target: str = ""
    raw_command: str = ""


_URL_RE = re.compile(r"https?://[^\s]+", re.I)


def parse_intent(command: str, app_aliases: dict[str, str], url_aliases: dict[str, str]) -> Intent:
    text = command.strip()
    if not text:
        return Intent(IntentType.UNKNOWN, raw_command=text)

    if re.search(r"截图|截屏|截个图|截下图", text):
        return Intent(IntentType.SCREENSHOT, raw_command=text)

    if re.search(r"分析屏幕|看看屏幕|理解屏幕|我在干什么|看看我在|屏幕理解", text):
        return Intent(IntentType.ANALYZE_SCREEN, raw_command=text)

    if re.search(r"日报|今日总结|每日总结|今天总结|总结一下", text):
        return Intent(IntentType.DAILY_REPORT, raw_command=text)

    if re.search(r"时间线|活动记录", text):
        return Intent(IntentType.TIMELINE, raw_command=text)

    if re.search(r"统计|运行状态|状态", text):
        return Intent(IntentType.STATS, raw_command=text)

    if re.search(r"打开面板|打开网页面板|打开时间线网页|打开界面", text):
        return Intent(IntentType.OPEN_WEB_UI, raw_command=text)

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

    help_m = re.search(r"帮(?:我|忙)?(.+)", text)
    if help_m:
        inner = help_m.group(1).strip()
        return parse_intent(inner, app_aliases, url_aliases)

    return Intent(IntentType.UNKNOWN, raw_command=text)
