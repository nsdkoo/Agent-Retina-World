from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class Speaker:
    """语音播报（独立线程 + 队列，不阻塞主循环）。

    engine="sherpa" 用 vits 离线中文音色；否则回退 pyttsx3。
    on_play_start / on_play_end 用于通知音频循环屏蔽麦克风，防自听回环。
    """

    def __init__(
        self,
        enabled: bool = True,
        engine: str = "pyttsx3",
        tts_model_dir=None,
        on_play_start: Callable[[], None] | None = None,
        on_play_end: Callable[[], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.engine_name = engine
        self.tts_model_dir = tts_model_dir
        self.on_play_start = on_play_start
        self.on_play_end = on_play_end
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self._tts = None
        self._pyttsx = None

    def say(self, text: str) -> None:
        if not self.enabled or not text:
            return
        self._queue.put(text[:200])

    def stop(self) -> None:
        self._queue.put(None)

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                break
            try:
                if self.on_play_start:
                    self.on_play_start()
                self._play(text)
            except Exception as exc:
                logger.warning("播报失败: %s", exc)
            finally:
                if self.on_play_end:
                    self.on_play_end()

    def _play(self, text: str) -> None:
        if self.engine_name == "sherpa" and self.tts_model_dir:
            if self._tts is None:
                from screen_agent.voice.sherpa_engine import SherpaTts

                self._tts = SherpaTts(self.tts_model_dir)
            samples, sample_rate = self._tts.synthesize(text)
            import sounddevice as sd

            sd.play(samples, sample_rate)
            sd.wait()
            return

        if self._pyttsx is None:
            import pyttsx3

            self._pyttsx = pyttsx3.init()
            self._pyttsx.setProperty("rate", 180)
        self._pyttsx.say(text)
        self._pyttsx.runAndWait()
