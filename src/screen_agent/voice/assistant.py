from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

from screen_agent.config import load_yaml
from screen_agent.pipeline import PerceptionPipeline
from screen_agent.voice.executor import ActionResult, CommandExecutor
from screen_agent.voice.intents import parse_intent
from screen_agent.voice.listener import Speaker, SpeechListener

logger = logging.getLogger(__name__)


class VoiceAssistant:
    """常驻语音助手：呼唤名字 → 语音指令 → 直接执行。"""

    def __init__(self, config_path: Path) -> None:
        raw = load_yaml(config_path)
        voice_cfg = raw.get("voice", {})
        web_cfg = raw.get("web", {})

        self.wake_names: list[str] = voice_cfg.get("wake_names", ["Retina", "小光", "光光"])
        self.pipeline = PerceptionPipeline.from_config(config_path)
        web_host = web_cfg.get("host", "127.0.0.1")
        web_port = int(web_cfg.get("port", 8765))
        self.web_url = f"http://{web_host}:{web_port}"

        self.listener = SpeechListener(
            language=voice_cfg.get("language", "zh-CN"),
            timeout=float(voice_cfg.get("listen_timeout", 8)),
            phrase_limit=float(voice_cfg.get("phrase_limit", 12)),
            energy_threshold=int(voice_cfg.get("energy_threshold", 300)),
        )
        self.speaker = Speaker(enabled=bool(voice_cfg.get("speak_feedback", True)))
        self.executor = CommandExecutor(self.pipeline, web_url=self.web_url)
        self.app_aliases: dict[str, str] = voice_cfg.get("apps", {})
        self.url_aliases: dict[str, str] = voice_cfg.get("urls", {})

        self._status = "idle"
        self._last_transcript = ""
        self._last_result = ""
        self._running = False
        self._on_status: Callable[[str], None] | None = None
        self._on_transcript: Callable[[str], None] | None = None
        self._on_result: Callable[[str], None] | None = None

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._on_status = cb

    def on_transcript(self, cb: Callable[[str], None]) -> None:
        self._on_transcript = cb

    def on_result(self, cb: Callable[[str], None]) -> None:
        self._on_result = cb

    def _set_status(self, status: str) -> None:
        self._status = status
        if self._on_status:
            self._on_status(status)

    def _emit_transcript(self, text: str) -> None:
        self._last_transcript = text
        if self._on_transcript:
            self._on_transcript(text)

    def _emit_result(self, text: str) -> None:
        self._last_result = text
        if self._on_result:
            self._on_result(text)

    def _contains_wake_word(self, text: str) -> bool:
        lower = text.lower()
        for name in self.wake_names:
            if name.lower() in lower or name in text:
                return True
        return False

    def _strip_wake_word(self, text: str) -> str:
        result = text
        for name in sorted(self.wake_names, key=len, reverse=True):
            result = re.sub(re.escape(name), "", result, flags=re.I)
        result = re.sub(r"^[，,、\s]+", "", result)
        result = re.sub(r"[，,、\s]+$", "", result)
        return result.strip()

    def process_text(self, text: str) -> ActionResult | None:
        if not self._contains_wake_word(text):
            return None
        command = self._strip_wake_word(text)
        if not command:
            return ActionResult(success=False, message="我在，请说你要做什么")
        intent = parse_intent(command, self.app_aliases, self.url_aliases)
        return self.executor.run(intent)

    def run_forever(self) -> None:
        self._running = True
        names = "、".join(f"「{n}」" for n in self.wake_names[:3])
        self._emit_result(f"语音助手已启动，呼唤 {names} 后说指令")
        self.speaker.say("语音助手已就绪")

        while self._running:
            self._set_status("idle")
            text = self.listener.listen_once(on_listening=lambda: self._set_status("listening"))
            if not text:
                continue
            self._emit_transcript(text)
            if not self._contains_wake_word(text):
                continue

            self._set_status("processing")
            result = self.process_text(text)
            if result is None:
                continue
            self._emit_result(result.message)
            if result.success:
                self.speaker.say(result.message)
            else:
                self.speaker.say(result.message)
            self._set_status("idle")
            time.sleep(0.3)

    def stop(self) -> None:
        self._running = False

    def run_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, daemon=True)
        thread.start()
        return thread
