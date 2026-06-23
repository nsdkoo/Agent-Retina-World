from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


class SpeechListener:
    """麦克风语音听写（中文）。"""

    def __init__(
        self,
        language: str = "zh-CN",
        timeout: float = 8.0,
        phrase_limit: float = 12.0,
        energy_threshold: int = 300,
    ) -> None:
        self.language = language
        self.timeout = timeout
        self.phrase_limit = phrase_limit
        self.energy_threshold = energy_threshold
        self._recognizer = None
        self._microphone = None

    def _ensure_deps(self) -> None:
        if self._recognizer is not None:
            return
        import speech_recognition as sr

        self._recognizer = sr.Recognizer()
        self._recognizer.energy_threshold = self.energy_threshold
        self._recognizer.dynamic_energy_threshold = True
        self._microphone = sr.Microphone()

    def listen_once(self, on_listening: Callable[[], None] | None = None) -> str | None:
        self._ensure_deps()
        import speech_recognition as sr

        assert self._recognizer is not None
        assert self._microphone is not None

        try:
            with self._microphone as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.4)
                if on_listening:
                    on_listening()
                audio = self._recognizer.listen(
                    source,
                    timeout=self.timeout,
                    phrase_time_limit=self.phrase_limit,
                )
            text = self._recognizer.recognize_google(audio, language=self.language)
            return text.strip()
        except sr.WaitTimeoutError:
            return None
        except sr.UnknownValueError:
            return None
        except Exception as exc:
            logger.warning("语音识别失败: %s", exc)
            return None


class Speaker:
    """语音播报反馈（可选）。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._engine = None

    def _ensure_engine(self) -> None:
        if not self.enabled or self._engine is not None:
            return
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", 180)
        except Exception as exc:
            logger.warning("TTS 不可用: %s", exc)
            self.enabled = False

    def say(self, text: str) -> None:
        if not self.enabled or not text:
            return
        self._ensure_engine()
        if self._engine is None:
            return
        try:
            self._engine.say(text[:120])
            self._engine.runAndWait()
        except Exception as exc:
            logger.warning("播报失败: %s", exc)
