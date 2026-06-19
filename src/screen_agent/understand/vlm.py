from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from screen_agent.capture.context import ForegroundContext, get_foreground_context


class PageCategory(str, Enum):
    BROWSER = "browser"
    IDE = "ide"
    TERMINAL = "terminal"
    CHAT = "chat"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    VIDEO = "video"
    SETTINGS = "settings"
    FILE_MANAGER = "file_manager"
    OTHER = "other"


class UserAction(str, Enum):
    READING = "reading"
    TYPING = "typing"
    BROWSING = "browsing"
    DEBUGGING = "debugging"
    MEETING = "meeting"
    SEARCHING = "searching"
    COPY_PASTE = "copy_paste"
    SCROLLING = "scrolling"
    IDLE = "idle"
    SWITCH_APP = "switch_app"
    DOWNLOAD = "download"
    UNKNOWN = "unknown"


@dataclass
class PageUnderstanding:
    screenshot_path: str
    analyzed_at: datetime
    page_category: PageCategory
    text_blocks: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    user_action: UserAction = UserAction.UNKNOWN
    high_value_events: list[str] = field(default_factory=list)
    summary: str = ""
    window_title: str = ""
    process_name: str = ""

    def to_dict(self) -> dict:
        return {
            "screenshot_path": self.screenshot_path,
            "analyzed_at": self.analyzed_at.isoformat(),
            "page_category": self.page_category.value,
            "text_blocks": self.text_blocks,
            "entities": self.entities,
            "user_action": self.user_action.value,
            "high_value_events": self.high_value_events,
            "summary": self.summary,
            "window_title": self.window_title,
            "process_name": self.process_name,
        }


class VLMAnalyzer(ABC):
    @abstractmethod
    def analyze(self, screenshot_path: Path) -> PageUnderstanding:
        ...


def _safe_enum(enum_cls: type[Enum], value: str, default: Enum) -> Enum:
    try:
        return enum_cls(value)
    except ValueError:
        return default


class HeuristicAnalyzer(VLMAnalyzer):
    """基于前台窗口与进程名的启发式理解（无需 VLM API）。"""

    _RULES: list[tuple[re.Pattern[str], PageCategory, UserAction]] = [
        (re.compile(r"chrome|msedge|firefox|edge|browser", re.I), PageCategory.BROWSER, UserAction.BROWSING),
        (re.compile(r"cursor|code\.exe|vscode|pycharm|idea64|devenv", re.I), PageCategory.IDE, UserAction.DEBUGGING),
        (re.compile(r"powershell|cmd\.exe|windowsterminal|wt\.exe|bash", re.I), PageCategory.TERMINAL, UserAction.TYPING),
        (re.compile(r"wechat|weixin|slack|discord|feishu|lark", re.I), PageCategory.CHAT, UserAction.READING),
        (re.compile(r"word|winword|excel|powerpnt|wps", re.I), PageCategory.DOCUMENT, UserAction.READING),
        (re.compile(r"explorer\.exe", re.I), PageCategory.FILE_MANAGER, UserAction.BROWSING),
        (re.compile(r"vlc|potplayer|bilibili|youtube", re.I), PageCategory.VIDEO, UserAction.READING),
        (re.compile(r"settings|控制面板|设置", re.I), PageCategory.SETTINGS, UserAction.READING),
    ]

    def analyze(self, screenshot_path: Path) -> PageUnderstanding:
        ctx = get_foreground_context()
        hint = ctx.hint_text if ctx else screenshot_path.name.lower()
        title = ctx.window_title if ctx else ""
        process = ctx.process_name or "" if ctx else ""

        category = PageCategory.OTHER
        action = UserAction.UNKNOWN
        for pattern, cat, act in self._RULES:
            if pattern.search(hint):
                category, action = cat, act
                break

        entities = [e for e in (title, process) if e]
        events: list[str] = []
        if title:
            events.append(f"foreground:{title[:80]}")

        summary = f"{category.value} / {action.value}"
        if title:
            summary += f" — {title[:60]}"

        return PageUnderstanding(
            screenshot_path=str(screenshot_path),
            analyzed_at=datetime.now(),
            page_category=category,
            text_blocks=[title] if title else [],
            entities=entities,
            user_action=action,
            high_value_events=events,
            summary=summary,
            window_title=title,
            process_name=process,
        )


# 兼容旧名称
MockVLMAnalyzer = HeuristicAnalyzer


class OpenAICompatibleVLMAnalyzer(VLMAnalyzer):
    """对接 OpenAI 兼容 VLM API。"""

    PROMPT = """分析这张桌面截图，返回 JSON：
{
  "page_category": "browser|ide|terminal|chat|document|...",
  "text_blocks": ["..."],
  "entities": ["..."],
  "user_action": "reading|typing|browsing|...",
  "high_value_events": ["..."],
  "summary": "一句话摘要"
}"""

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=120.0)

    def analyze(self, screenshot_path: Path) -> PageUnderstanding:
        import base64

        ctx = get_foreground_context()
        raw = screenshot_path.read_bytes()
        b64 = base64.b64encode(raw).decode()
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = self._client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)

        return PageUnderstanding(
            screenshot_path=str(screenshot_path),
            analyzed_at=datetime.now(),
            page_category=_safe_enum(PageCategory, data.get("page_category", "other"), PageCategory.OTHER),
            text_blocks=data.get("text_blocks", []),
            entities=data.get("entities", []),
            user_action=_safe_enum(UserAction, data.get("user_action", "unknown"), UserAction.UNKNOWN),
            high_value_events=data.get("high_value_events", []),
            summary=data.get("summary", ""),
            window_title=ctx.window_title if ctx else "",
            process_name=ctx.process_name or "" if ctx else "",
        )
