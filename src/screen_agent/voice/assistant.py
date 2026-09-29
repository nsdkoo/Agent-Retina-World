from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

from screen_agent.config import load_yaml
from screen_agent.pipeline import PerceptionPipeline
from screen_agent.understand.chat import build_chat_client
from screen_agent.voice.executor import ActionResult, CommandExecutor
from screen_agent.voice.intents import IntentType, parse_intent
from screen_agent.voice.listener import Speaker

logger = logging.getLogger(__name__)


class VoiceAssistant:
    """常驻语音助手：唤醒后可进入免唤醒连续对话，语音与打字共用 handle_command。"""

    def __init__(self, config_path: Path, project_root: Path | None = None) -> None:
        raw = load_yaml(config_path)
        voice_cfg = raw.get("voice", {})
        chat_cfg = raw.get("chat", {})
        web_cfg = raw.get("web", {})
        sherpa_cfg = voice_cfg.get("sherpa", {}) if isinstance(voice_cfg.get("sherpa", {}), dict) else {}
        root = project_root or config_path.parent

        self.wake_names: list[str] = voice_cfg.get("wake_names", ["Retina", "小光", "光光"])
        self.session_enabled = bool(voice_cfg.get("session_mode", True))
        self.session_duration = float(voice_cfg.get("session_duration_seconds", 60))
        self.chat_enabled = bool(chat_cfg.get("enabled", False))
        self.chat_model = self._chat_model_hint(chat_cfg)

        self.pipeline = PerceptionPipeline.from_config(config_path)
        web_host = web_cfg.get("host", "127.0.0.1")
        web_port = int(web_cfg.get("port", 8765))
        self.web_url = f"http://{web_host}:{web_port}"

        # ---- 语音引擎（sherpa 流式栈） ----
        models_root = root / sherpa_cfg.get("models_dir", "models")
        from screen_agent.voice.audio_loop import AudioLoop
        from screen_agent.voice.sherpa_engine import build_keywords_txt

        asr_dir = models_root / sherpa_cfg.get(
            "asr_model_dir", "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
        )
        kws_dir = models_root / sherpa_cfg.get(
            "kws_model_dir", "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        )
        tts_dir = models_root / sherpa_cfg.get("tts_model_dir", "vits-zh-hf-fanchen-C")
        keywords_file = root / "data" / "keywords.txt"
        keywords = list(dict.fromkeys(
            list(sherpa_cfg.get("keywords", ["小光", "光光"])) + [
                w for w in self.wake_names if not w.isascii()
            ]
        ))
        kws_threshold = float(sherpa_cfg.get("kws_threshold", 0.4))
        build_keywords_txt(keywords, kws_dir, keywords_file, kws_threshold)

        self.audio_loop = AudioLoop(
            self,
            asr_model_dir=asr_dir,
            kws_model_dir=kws_dir,
            keywords_file=keywords_file,
            kws_threshold=kws_threshold,
            num_threads=int(sherpa_cfg.get("num_threads", 1)),
            mic_device=sherpa_cfg.get("mic_device"),
            idle_asr=bool(sherpa_cfg.get("idle_asr", False)),
            audio_file=(root / sherpa_cfg["audio_source"])
            if sherpa_cfg.get("audio_source")
            else None,
        )

        tts_engine = sherpa_cfg.get("tts_engine", "sherpa")
        self.speaker = Speaker(
            enabled=bool(voice_cfg.get("speak_feedback", True)),
            engine=tts_engine,
            tts_model_dir=tts_dir if tts_dir.is_dir() else None,
            on_play_start=self.audio_loop.set_muted_mic,
            on_play_end=self.audio_loop.set_unmuted_mic,
        )

        self._chat_history: list[dict[str, str]] = []
        chat_client = build_chat_client(chat_cfg)
        self.executor = CommandExecutor(
            self.pipeline,
            web_url=self.web_url,
            chat_client=chat_client,
            chat_history=self._chat_history,
            max_history=int(chat_cfg.get("max_history", 6)),
            screen_context_fn=self._recent_screen_context,
            on_chat_delta=self._emit_result_delta,
        )
        self.app_aliases: dict[str, str] = voice_cfg.get("apps", {})
        self.url_aliases: dict[str, str] = voice_cfg.get("urls", {})

        self._in_session = False
        self._session_until = 0.0
        self._status = "idle"
        self._running = False
        self._on_status: Callable[[str], None] | None = None
        self._on_transcript: Callable[[str], None] | None = None
        self._on_result: Callable[[str], None] | None = None
        self._on_result_delta: Callable[[str], None] | None = None
        self._on_session: Callable[[bool], None] | None = None

    @staticmethod
    def _chat_model_hint(chat_cfg: dict) -> str:
        backends = chat_cfg.get("backends")
        if isinstance(backends, list) and backends:
            return " + ".join(
                str(b.get("model", "?")) for b in backends if isinstance(b, dict)
            )
        return str(chat_cfg.get("model", "gpt-5.4-mini"))

    def _recent_screen_context(self) -> str:
        try:
            md = self.pipeline.proactive.timeline_markdown(limit=1)
            lines = [ln.lstrip("- ").strip() for ln in md.split("\n") if ln.startswith("- ")]
            return lines[0][:120] if lines else ""
        except Exception:
            return ""

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._on_status = cb

    def on_transcript(self, cb: Callable[[str], None]) -> None:
        self._on_transcript = cb

    def on_result(self, cb: Callable[[str], None]) -> None:
        self._on_result = cb

    def on_result_delta(self, cb: Callable[[str], None]) -> None:
        """注册流式增量回调（累计全文）。UI 用来边生成边显示。"""
        self._on_result_delta = cb

    def _emit_result_delta(self, accumulated: str) -> None:
        """节流转发：~90ms 一次，避免高频信号打爆 UI 事件循环；最终全文由 emit_result 兜底。"""
        now = time.monotonic()
        if now - getattr(self, "_last_delta_emit", 0.0) < 0.09:
            return
        self._last_delta_emit = now
        if self._on_result_delta is None:
            return
        try:
            self._on_result_delta(accumulated)
        except Exception:
            logger.debug("delta 回调异常", exc_info=True)

    def on_session(self, cb: Callable[[bool], None]) -> None:
        self._on_session = cb

    @property
    def in_session(self) -> bool:
        return self._in_session and time.time() < self._session_until

    @property
    def session_expired(self) -> bool:
        """会话开着但已超时（由音频循环检测并收尾）。"""
        return self._in_session and not self.in_session

    # ---- 供 AudioLoop / UI 调用的公共接口 ----

    def set_status(self, status: str) -> None:
        self._status = status
        if self._on_status:
            self._on_status(status)

    def emit_transcript(self, text: str) -> None:
        if self._on_transcript:
            self._on_transcript(text)

    def emit_result(self, text: str) -> None:
        if self._on_result:
            self._on_result(text)

    def speak(self, text: str) -> None:
        self.speaker.say(text)

    def contains_wake_word(self, text: str) -> bool:
        lower = text.lower()
        for name in self.wake_names:
            if name.lower() in lower or name in text:
                return True
        return False

    def strip_wake_word(self, text: str) -> str:
        result = text
        for name in sorted(self.wake_names, key=len, reverse=True):
            result = re.sub(re.escape(name), "", result, flags=re.I)
        result = re.sub(r"^[，,、\s]+", "", result)
        result = re.sub(r"[，,、\s]+$", "", result)
        return result.strip()

    def begin_wake_session(self) -> str:
        """KWS 命中且后面没有跟命令时调用。返回播报文案。"""
        if self.session_enabled:
            self._set_session(True)
            return "我在，请说你要做什么"
        self.set_status("listening")
        return "我在"

    def handle_command(self, command: str) -> ActionResult | None:
        """执行一条已剥离唤醒词的指令（语音与打字共用入口）。"""
        command = (command or "").strip()
        if not command:
            return None
        intent = parse_intent(command, self.app_aliases, self.url_aliases)
        result = self.executor.run(intent)
        if intent.type == IntentType.END_SESSION:
            self._set_session(False)
        elif self.session_enabled and not self._in_session:
            self._set_session(True)
        else:
            self._extend_session()
        return result

    def end_session(self) -> None:
        self._set_session(False)

    # ---- 兼容保留：直接处理一条完整转写（打字面板可走这里） ----

    def _set_status(self, status: str) -> None:
        self.set_status(status)

    def _emit_transcript(self, text: str) -> None:
        self.emit_transcript(text)

    def _emit_result(self, text: str) -> None:
        self.emit_result(text)

    def _contains_wake_word(self, text: str) -> bool:
        return self.contains_wake_word(text)

    def _strip_wake_word(self, text: str) -> str:
        return self.strip_wake_word(text)

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

        if not self.contains_wake_word(text):
            return None

        command = self.strip_wake_word(text)
        if not command:
            if self.session_enabled:
                self._set_session(True)
            return ActionResult(success=True, message="我在，请说你要做什么")

        intent = parse_intent(command, self.app_aliases, self.url_aliases)
        result = self.executor.run(intent)
        if self.session_enabled and intent.type != IntentType.END_SESSION:
            self._set_session(True)
        return result

    def _set_session(self, active: bool) -> None:
        self._in_session = active
        if active:
            self._session_until = time.time() + self.session_duration
            self.set_status("session")
        else:
            self._session_until = 0.0
            self._chat_history.clear()
            self.set_status("idle")
        if self._on_session:
            self._on_session(active)

    def _extend_session(self) -> None:
        if self._in_session:
            self._session_until = time.time() + self.session_duration

    def run_forever(self) -> None:
        self._running = True
        names = "、".join(f"「{n}」" for n in self.wake_names[:3])
        chat_hint = f" · 对话模型 {self.chat_model}" if self.chat_enabled else ""
        self.emit_result(f"语音助手已启动（离线流式识别{chat_hint}）· 呼唤 {names}")
        if self.session_enabled:
            self.emit_result("唤醒后进入连续对话，说「退出」结束")
        self.speak("语音助手已就绪")

        try:
            self.audio_loop.run()
        except RuntimeError as exc:
            logger.error("语音主循环退出: %s", exc)
            self.emit_result(str(exc).splitlines()[0])
        finally:
            self.speaker.stop()

    def stop(self) -> None:
        self._running = False
        self.audio_loop.stop()

    def run_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, daemon=True)
        thread.start()
        return thread
