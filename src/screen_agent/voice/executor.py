from __future__ import annotations

import logging
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from screen_agent.pipeline import PerceptionPipeline
from screen_agent.understand.chat import DisabledChatClient, OpenAICompatibleChatClient, SYSTEM_PROMPT
from screen_agent.voice.intents import Intent, IntentType

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    success: bool
    message: str
    detail: dict | None = None


class CommandExecutor:
    def __init__(
        self,
        pipeline: PerceptionPipeline,
        web_url: str = "http://127.0.0.1:8765",
        chat_client: OpenAICompatibleChatClient | DisabledChatClient | None = None,
        chat_history: list[dict[str, str]] | None = None,
        max_history: int = 6,
        screen_context_fn: Callable[[], str] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.web_url = web_url
        self.chat_client = chat_client or DisabledChatClient()
        self.chat_history = chat_history if chat_history is not None else []
        self.max_history = max_history
        self._screen_context_fn = screen_context_fn

    def run(self, intent: Intent) -> ActionResult:
        handlers = {
            IntentType.SCREENSHOT: self._screenshot,
            IntentType.ANALYZE_SCREEN: self._analyze,
            IntentType.OPEN_URL: self._open_url,
            IntentType.OPEN_APP: self._open_app,
            IntentType.DAILY_REPORT: self._report,
            IntentType.TIMELINE: self._timeline,
            IntentType.STATS: self._stats,
            IntentType.OPEN_WEB_UI: self._open_web_ui,
            IntentType.END_SESSION: self._end_session,
            IntentType.CHAT: self._chat,
        }
        handler = handlers.get(intent.type)
        if not handler:
            return ActionResult(
                success=False,
                message="我还没学会这个，可以说：截图、打开百度、分析屏幕、今日总结",
            )
        try:
            return handler(intent)
        except Exception as exc:
            logger.exception("命令执行失败")
            return ActionResult(success=False, message=f"执行失败：{exc}")

    def _chat(self, intent: Intent) -> ActionResult:
        if isinstance(self.chat_client, DisabledChatClient):
            return ActionResult(
                success=False,
                message="对话未配置，请设置 chat.enabled 和 OPENAI_API_KEY",
            )

        user_text = intent.raw_command.strip()
        if not user_text:
            return ActionResult(success=False, message="我没听清，请再说一遍")

        system = SYSTEM_PROMPT
        if self._screen_context_fn:
            ctx = self._screen_context_fn()
            if ctx:
                system = f"{SYSTEM_PROMPT}\n\n最近屏幕活动：{ctx}"

        messages = list(self.chat_history)
        messages.append({"role": "user", "content": user_text})

        try:
            reply = self.chat_client.complete(messages, system=system)
        except Exception as exc:
            logger.exception("Chat 请求失败")
            return ActionResult(success=False, message=f"对话失败：{exc}")

        self.chat_history.append({"role": "user", "content": user_text})
        self.chat_history.append({"role": "assistant", "content": reply})
        if len(self.chat_history) > self.max_history:
            del self.chat_history[: len(self.chat_history) - self.max_history]

        return ActionResult(success=True, message=reply, detail={"chat": True})

    def _screenshot(self, intent: Intent) -> ActionResult:
        result = self.pipeline.run_once()
        self.pipeline.flush()
        if result.get("status") == "skipped_duplicate":
            return ActionResult(success=True, message="屏幕没变化，这次不用截", detail=result)
        summary = result.get("summary", "已完成")
        return ActionResult(success=True, message=f"已截图并理解：{summary}", detail=result)

    def _analyze(self, intent: Intent) -> ActionResult:
        return self._screenshot(intent)

    def _open_url(self, intent: Intent) -> ActionResult:
        url = intent.target
        webbrowser.open(url)
        return ActionResult(success=True, message=f"已打开网页", detail={"url": url})

    def _open_app(self, intent: Intent) -> ActionResult:
        target = intent.target
        if Path(target).exists():
            subprocess.Popen([target], shell=False)
        else:
            subprocess.Popen(f'start "" "{target}"', shell=True)
        return ActionResult(success=True, message=f"正在打开 {target}", detail={"app": target})

    def _report(self, intent: Intent) -> ActionResult:
        text = self.pipeline.proactive.daily_summary()
        todos = self.pipeline.proactive.todo_suggestions()
        preview = text.split("\n")[0][:40]
        return ActionResult(
            success=True,
            message=f"日报已生成。{preview}",
            detail={"todos": todos},
        )

    def _timeline(self, intent: Intent) -> ActionResult:
        md = self.pipeline.proactive.timeline_markdown(limit=5)
        lines = [ln for ln in md.split("\n") if ln.startswith("- ")]
        preview = lines[0][:50] if lines else "暂无记录"
        return ActionResult(success=True, message=f"最近活动：{preview}", detail={"timeline": md})

    def _stats(self, intent: Intent) -> ActionResult:
        stats = self.pipeline.runtime_stats()
        p = stats.get("pipeline", {})
        msg = f"已采集{p.get('captured', 0)}帧，去重跳过{p.get('dedup_skipped', 0)}次"
        return ActionResult(success=True, message=msg, detail=stats)

    def _open_web_ui(self, intent: Intent) -> ActionResult:
        webbrowser.open(self.web_url)
        return ActionResult(success=True, message="已打开时间线面板", detail={"url": self.web_url})

    def _end_session(self, intent: Intent) -> ActionResult:
        self.chat_history.clear()
        return ActionResult(success=True, message="好的，有事再叫我", detail={"end_session": True})
