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
        # 截断放宽到 500 字，且尽量断在句末（不再无声砍半句）
        if len(text) > 500:
            cut = text[:500]
            for sep in ('。', '！', '？', '；', '.'):
                pos = cut.rfind(sep)
                if pos > 200:
                    cut = cut[:pos + 1]
                    break
            text = cut + '……（后面还有，看面板全文）'
        self._queue.put(text)

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
        if self.engine_name == "edge":
            try:
                self._play_edge(text)
                return
            except Exception as exc:
                logger.warning("edge-tts 失败（%s），降级本地播报", exc)
        if self.engine_name in ("edge", "sherpa") and self.tts_model_dir:
            if self._tts is None:
                from screen_agent.voice.sherpa_engine import SherpaTts

                self._tts = SherpaTts(self.tts_model_dir)
            samples, sample_rate = self._tts.synthesize(text)
            import sounddevice as sd

            sd.play(samples, sample_rate)
            sd.wait()
            return

    def _play_edge(self, text: str) -> None:
        """edge-tts 微软自然音色（免费）：合成 mp3 → miniaudio 解码 → 播放。"""
        import asyncio

        import edge_tts
        import miniaudio

        async def _synth() -> bytes:
            com = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
            chunks = bytearray()
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    chunks += chunk["data"]
            return bytes(chunks)

        mp3 = asyncio.run(_synth())
        decoded = miniaudio.decode(mp3)
        import numpy as np

        import sounddevice as sd

        samples = np.array(decoded.samples, dtype=np.float32) / 32768.0
        sd.play(samples, decoded.sample_rate)
        sd.wait()

        if self._pyttsx is None:
            import pyttsx3

            self._pyttsx = pyttsx3.init()
            self._pyttsx.setProperty("rate", 180)
        self._pyttsx.say(text)
        self._pyttsx.runAndWait()
