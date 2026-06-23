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
from screen_agent.voice.intents import IntentType, parse_intent
from screen_agent.voice.listener import Speaker, SpeechListener
from screen_agent.voice.offline_stt import resolve_stt_engine

logger = logging.getLogger(__name__)


class VoiceAssistant:
    """常驻语音助手：唤醒后可进入免唤醒连续对话。"""

    def __init__(self, config_path: Path, project_root: Path | None = None) -> None:
        raw = load_yaml(config_path)
        voice_cfg = raw.get("voice", {})
        web_cfg = raw.get("web", {})
        root = project_root or config_path.parent

        self.wake_names: list[str] = voice_cfg.get("wake_names", ["Retina", "小光", "光光"])
        self.session_enabled = bool(voice_cfg.get("session_mode", True))
        self.session_duration = float(voice_cfg.get("session_duration_seconds", 60))
        self.stt_engine_name = voice_cfg.get("stt_engine", "auto")

        self.pipeline = PerceptionPipeline.from_config(config_path)
        web_host = web_cfg.get("host", "127.0.0.1")
        web_port = int(web_cfg.get("port", 8765))
        self.web_url = f"http://{web_host}:{web_port}"

        model_path = root / voice_cfg.get("vosk_model_path", "models/vosk-model-small-cn-0.22")
        stt = resolve_stt_engine(
            self.stt_engine_name,
            model_path,
            voice_cfg.get("language", "zh-CN"),
        )
        self.listener = SpeechListener(
            engine=stt,
            timeout=float(voice_cfg.get("listen_timeout", 8)),
            phrase_limit=float(voice_cfg.get("phrase_limit", 12)),
            energy_threshold=int(voice_cfg.get("energy_threshold", 300)),
        )
        self.speaker = Speaker(enabled=bool(voice_cfg.get("speak_feedback", True)))
        self.executor = CommandExecutor(self.pipeline, web_url=self.web_url)
        self.app_aliases: dict[str, str] = voice_cfg.get("apps", {})
        self.url_aliases: dict[str, str] = voice_cfg.get("urls", {})

        self._in_session = False
        self._session_until = 0.0
        self._status = "idle"
        self._running = False
        self._on_status: Callable[[str], None] | None = None
        self._on_transcript: Callable[[str], None] | None = None
        self._on_result: Callable[[str], None] | None = None
        self._on_session: Callable[[bool], None] | None = None

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._on_status = cb

    def on_transcript(self, cb: Callable[[str], None]) -> None:
        self._on_transcript = cb

    def on_result(self, cb: Callable[[str], None]) -> None:
        self._on_result = cb

    def on_session(self, cb: Callable[[bool], None]) -> None:
        self._on_session = cb

    @property
    def in_session(self) -> bool:
        return self._in_session and time.time() < self._session_until

    def _set_status(self, status: str) -> None:
        self._status = status
        if self._on_status:
            self._on_status(status)

    def _emit_transcript(self, text: str) -> None:
        if self._on_transcript:
            self._on_transcript(text)

    def _emit_result(self, text: str) -> None:
        if self._on_result:
            self._on_result(text)

    def _set_session(self, active: bool) -> None:
        self._in_session = active
        if active:
            self._session_until = time.time() + self.session_duration
            self._set_status("session")
        else:
            self._session_until = 0.0
            self._set_status("idle")
        if self._on_session:
            self._on_session(active)

    def _extend_session(self) -> None:
        if self._in_session:
            self._session_until = time.time() + self.session_duration

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

    def handle_transcript(self, text: str) -> ActionResult | None:
        """处理一条语音转写。会话中免唤醒。"""
        if self.in_session:
            command = text.strip()
            if not command:
                return None
            intent = parse_intent(command, self.app_aliases, self.url_aliases)
            result = self.executor.run(intent)
            if intent.type == IntentType.END_SESSION:
                self._set_session(False)
            else:
                self._extend_session()
            return result

        if not self._contains_wake_word(text):
            return None

        command = self._strip_wake_word(text)
        if not command:
            if self.session_enabled:
                self._set_session(True)
            return ActionResult(success=True, message="我在，请说你要做什么")

        intent = parse_intent(command, self.app_aliases, self.url_aliases)
        result = self.executor.run(intent)
        if self.session_enabled and intent.type != IntentType.END_SESSION:
            self._set_session(True)
        return result

    def run_forever(self) -> None:
        self._running = True
        names = "、".join(f"「{n}」" for n in self.wake_names[:3])
        engine_hint = "离线" if self.stt_engine_name == "vosk" else "在线/离线自动"
        self._emit_result(f"语音助手已启动（{engine_hint}）· 呼唤 {names}")
        if self.session_enabled:
            self._emit_result("唤醒后进入连续对话，说「退出」结束")
        self.speaker.say("语音助手已就绪")

        while self._running:
            if self.in_session:
                self._set_status("session")
            else:
                self._set_status("idle")

            text = self.listener.listen_once(on_listening=lambda: self._set_status("listening"))
            if not text:
                if self.in_session and time.time() >= self._session_until:
                    self._set_session(False)
                    self._emit_result("连续对话已超时结束")
                continue

            self._emit_transcript(text)
            self._set_status("processing")
            result = self.handle_transcript(text)
            if result is None:
                self._set_status("session" if self.in_session else "idle")
                continue

            self._emit_result(result.message)
            self.speaker.say(result.message)
            self._set_status("session" if self.in_session else "idle")
            time.sleep(0.2)

    def stop(self) -> None:
        self._running = False

    def run_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, daemon=True)
        thread.start()
        return thread
