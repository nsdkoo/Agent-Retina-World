from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from screen_agent.capture.context import get_foreground_context

logger = logging.getLogger(__name__)

PAGE_CATEGORIES = [
    "browser", "ide", "terminal", "chat", "document",
    "spreadsheet", "video", "settings", "file_manager", "other",
]
USER_ACTIONS = [
    "reading", "typing", "browsing", "debugging", "meeting",
    "searching", "copy_paste", "scrolling", "idle", "switch_app", "download", "unknown",
]


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


class VLMOutputSchema(BaseModel):
    page_category: str = "other"
    text_blocks: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    user_action: str = "unknown"
    high_value_events: list[str] = Field(default_factory=list)
    summary: str = ""


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
    source: str = "heuristic"

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
            "source": self.source,
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


def _from_schema(
    schema: VLMOutputSchema,
    screenshot_path: Path,
    ctx_title: str,
    ctx_process: str,
    source: str,
) -> PageUnderstanding:
    return PageUnderstanding(
        screenshot_path=str(screenshot_path),
        analyzed_at=datetime.now(),
        page_category=_safe_enum(PageCategory, schema.page_category, PageCategory.OTHER),
        text_blocks=schema.text_blocks,
        entities=schema.entities,
        user_action=_safe_enum(UserAction, schema.user_action, UserAction.UNKNOWN),
        high_value_events=schema.high_value_events,
        summary=schema.summary,
        window_title=ctx_title,
        process_name=ctx_process,
        source=source,
    )


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

        schema = VLMOutputSchema(
            page_category=category.value,
            text_blocks=[title] if title else [],
            entities=entities,
            user_action=action.value,
            high_value_events=events,
            summary=summary,
        )
        return _from_schema(schema, screenshot_path, title, process, "heuristic")


MockVLMAnalyzer = HeuristicAnalyzer


class OpenAICompatibleVLMAnalyzer(VLMAnalyzer):
    """对接 OpenAI 兼容多模态 VLM API（如 Qwen2.5-VL、GPT-4o）。"""

    PROMPT = f"""你是桌面屏幕理解助手。分析截图并**仅**返回 JSON，字段如下：
{{
  "page_category": "{'|'.join(PAGE_CATEGORIES)}",
  "text_blocks": ["可见关键文本片段"],
  "entities": ["文件名、应用名、关键词"],
  "user_action": "{'|'.join(USER_ACTIONS)}",
  "high_value_events": ["值得记录的事件"],
  "summary": "一句话描述用户正在做什么"
}}"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        max_retries: int = 2,
        timeout: float = 120.0,
    ) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)

    def _parse_response(self, content: str) -> VLMOutputSchema:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        return VLMOutputSchema.model_validate(data)

    def analyze(self, screenshot_path: Path) -> PageUnderstanding:
        import base64

        ctx = get_foreground_context()
        title = ctx.window_title if ctx else ""
        process = ctx.process_name or "" if ctx else ""
        raw = screenshot_path.read_bytes()
        b64 = base64.b64encode(raw).decode()

        context_hint = ""
        if title or process:
            context_hint = f"\n\n前台窗口：{title}\n进程：{process}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.PROMPT + context_hint},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_err: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                schema = self._parse_response(content)
                return _from_schema(schema, screenshot_path, title, process, "vlm")
            except (json.JSONDecodeError, ValidationError, KeyError, httpx.HTTPError) as exc:
                last_err = exc
                logger.warning("VLM 调用失败 attempt=%s: %s", attempt + 1, exc)

        raise RuntimeError(f"VLM 分析失败: {last_err}") from last_err


class FallbackVLMAnalyzer(VLMAnalyzer):
    """VLM 失败时自动降级启发式。"""

    def __init__(self, primary: VLMAnalyzer, fallback: HeuristicAnalyzer | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or HeuristicAnalyzer()

    def analyze(self, screenshot_path: Path) -> PageUnderstanding:
        try:
            return self.primary.analyze(screenshot_path)
        except Exception as exc:
            logger.warning("VLM 降级启发式: %s", exc)
            result = self.fallback.analyze(screenshot_path)
            result.source = "heuristic_fallback"
            return result


def build_analyzer(vlm_cfg: dict[str, Any]) -> VLMAnalyzer:
    from screen_agent.config import resolve_secret

    provider = vlm_cfg.get("provider", "heuristic")
    if provider != "openai_compatible":
        return HeuristicAnalyzer()

    api_key = resolve_secret(vlm_cfg.get("api_key"), vlm_cfg.get("api_key_env", "VLM_API_KEY"))
    primary = OpenAICompatibleVLMAnalyzer(
        base_url=vlm_cfg["base_url"],
        model=vlm_cfg["model"],
        api_key=api_key,
        max_retries=int(vlm_cfg.get("max_retries", 2)),
        timeout=float(vlm_cfg.get("timeout", 120)),
    )
    if vlm_cfg.get("fallback_heuristic", True):
        return FallbackVLMAnalyzer(primary)
    return primary
