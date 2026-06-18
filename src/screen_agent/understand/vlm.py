from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


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
        }


class VLMAnalyzer(ABC):
    @abstractmethod
    def analyze(self, screenshot_path: Path) -> PageUnderstanding:
        ...


class MockVLMAnalyzer(VLMAnalyzer):
    """无 API 时的启发式 Mock，便于本地跑通链路。"""

    _RULES: list[tuple[re.Pattern[str], PageCategory, UserAction]] = [
        (re.compile(r"chrome|edge|firefox|browser", re.I), PageCategory.BROWSER, UserAction.BROWSING),
        (re.compile(r"cursor|vscode|pycharm|idea", re.I), PageCategory.IDE, UserAction.DEBUGGING),
        (re.compile(r"powershell|cmd|terminal|bash", re.I), PageCategory.TERMINAL, UserAction.TYPING),
        (re.compile(r"wechat|slack|discord|飞书", re.I), PageCategory.CHAT, UserAction.READING),
    ]

    def analyze(self, screenshot_path: Path) -> PageUnderstanding:
        name = screenshot_path.name.lower()
        category = PageCategory.OTHER
        action = UserAction.UNKNOWN
        for pattern, cat, act in self._RULES:
            if pattern.search(name):
                category, action = cat, act
                break

        return PageUnderstanding(
            screenshot_path=str(screenshot_path),
            analyzed_at=datetime.now(),
            page_category=category,
            text_blocks=[f"mock-block-from-{screenshot_path.name}"],
            entities=["mock-entity"],
            user_action=action,
            high_value_events=["mock_event:screen_captured"],
            summary=f"Mock 理解：{category.value} / {action.value}",
        )


class OpenAICompatibleVLMAnalyzer(VLMAnalyzer):
    """对接 OpenAI 兼容 VLM API（如 Qwen2.5-VL）。"""

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
            page_category=PageCategory(data.get("page_category", "other")),
            text_blocks=data.get("text_blocks", []),
            entities=data.get("entities", []),
            user_action=UserAction(data.get("user_action", "unknown")),
            high_value_events=data.get("high_value_events", []),
            summary=data.get("summary", ""),
        )
