"""常驻流式音频主循环：KWS 唤醒 → 流式识别 → 命令执行。

状态机（单推理线程，控制 CPU 占用）：
- IDLE      只喂 KWS（可选叠加 ASR 转写英文唤醒词），命中后进入 LISTENING
- LISTENING 流式 ASR + endpoint 断句，出句后丢给命令执行线程池
- 播报期间 muted，音频直接丢弃防自听回环
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from screen_agent.voice.sherpa_engine import SAMPLE_RATE, SherpaAsr, SherpaKws

logger = logging.getLogger(__name__)

BLOCK_SIZE = 480          # 30ms @ 16kHz
RING_SECONDS = 3.0        # 唤醒词回看缓冲
PARTIAL_EMIT_INTERVAL = 0.35
PHRASE_TIMEOUT = 8.0      # 监听中迟迟没人说话则退出
LISTENING_MAX = 15.0      # 单次监听最长时长


def list_microphones() -> list[dict]:
    """枚举输入设备，供配置 mic_device 参考。"""
    import sounddevice as sd

    devices = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            devices.append({"index": idx, "name": dev.get("name", ""), "channels": dev.get("max_input_channels")})
    return devices


def pick_input_device(mic_device=None):
    """选输入设备：显式配置优先；否则试开验证，挑第一个真正能开的（优先麦克风）。"""
    import sounddevice as sd

    if mic_device is not None:
        return mic_device
    inputs = [
        (idx, dev)
        for idx, dev in enumerate(sd.query_devices())
        if dev.get("max_input_channels", 0) > 0
    ]
    if not inputs:
        raise RuntimeError("没有可用的输入设备（麦克风可能未插入或被禁用）")

    def _try(idx):
        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE, dtype="int16",
                channels=1, device=idx,
            )
            stream.close()
            return True
        except Exception:
            return False

    for idx, dev in inputs:
        name = str(dev.get("name", ""))
        if ("麦克风" in name or "microphone" in name.lower()) and _try(idx):
            return idx
    for idx, _dev in inputs:
        if _try(idx):
            return idx
    raise RuntimeError(
        "没有可用的输入设备（麦克风可能未插入或在系统里被禁用）——"
        "插上麦克风后重启语音助手"
    )


class AudioLoop:
    """语音交互主循环。持有麦克风流与推理线程，经 assistant 公共接口驱动业务。"""

    def __init__(
        self,
        assistant,
        asr_model_dir: Path,
        kws_model_dir: Path,
        keywords_file: Path,
        kws_threshold: float = 0.4,
        num_threads: int = 1,
        mic_device=None,
        idle_asr: bool = False,
        hotwords_file: Path | None = None,
        audio_file: Path | None = None,
    ) -> None:
        self.assistant = assistant
        self.mic_device = mic_device
        self.idle_asr_enabled = idle_asr
        self.audio_file = audio_file

        self.asr = SherpaAsr(asr_model_dir, num_threads=num_threads, hotwords_file=hotwords_file)
        self._refiner = None   # SenseVoice 精修器（惰性加载）
        self._utt_chunks: list = []  # 当前语句的原始采样（供精修复核）
        self.kws = SherpaKws(
            kws_model_dir, keywords_file, threshold=kws_threshold, num_threads=num_threads
        )
        if idle_asr:
            logger.info("IDLE 双开 ASR 转写已启用（英文唤醒词兜底，CPU 占用更高）")

        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._ring: deque = deque(
            maxlen=int(RING_SECONDS * SAMPLE_RATE / BLOCK_SIZE)
        )
        self._cmd_q: queue.Queue = queue.Queue()
        self._muted = threading.Event()
        self._processing = threading.Event()
        self._running = threading.Event()

        self._state = "idle"
        self._kws_stream = self.kws.create_stream()
        self._asr_stream = None
        self._last_partial = ""
        self._last_emit = 0.0
        self._phrase_deadline = 0.0
        self._listening_started = 0.0

        self._mic_stream = None
        self._worker: threading.Thread | None = None
        self._level = 0.0  # 平滑音量 0-1，供 UI 做呼吸环

    def get_level(self) -> float:
        return min(1.0, self._level * 4.0)

    # ---- 麦克风采集（sounddevice 回调线程） ----

    def _on_audio(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if self._muted.is_set():
            return
        block = indata[:, 0].astype(np.float32) / 32768.0
        try:
            self._q.put_nowait(block)
        except queue.Full:
            pass

    def set_muted(self, muted: bool) -> None:
        if muted:
            self._muted.set()
        else:
            self._muted.clear()
            # 清掉播报期间积压的旧音频
            try:
                while True:
                    self._q.get_nowait()
            except queue.Empty:
                pass

    def set_muted_mic(self) -> None:
        self.set_muted(True)

    def set_unmuted_mic(self) -> None:
        self.set_muted(False)

    # ---- 命令执行线程 ----

    def _command_worker(self) -> None:
        while True:
            item = self._cmd_q.get()
            if item is None:
                break
            text = item
            try:
                self._processing.set()
                self.assistant.set_status("processing")
                result = self.assistant.handle_command(text)
                if result is not None:
                    self.assistant.emit_result(result.message)
                    self.assistant.speak(result.message)
                self.assistant.set_status("session" if self.assistant.in_session else "idle")
            except Exception:
                logger.exception("命令执行线程异常")
            finally:
                self._processing.clear()

    # ---- 主循环 ----

    def run(self) -> None:
        import sounddevice as sd

        self._running.set()
        self._worker = threading.Thread(target=self._command_worker, daemon=True)
        self._worker.start()

        try:
            if self.audio_file is not None:
                self._mic_stream = None
                self._start_file_source(self.audio_file)
                logger.info("使用音频文件作为输入源: %s", self.audio_file)
            else:
                device = pick_input_device(self.mic_device)
                logger.info("使用输入设备: %s", device)
                self._mic_stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=BLOCK_SIZE,
                    dtype="int16",
                    channels=1,
                    device=device,
                    callback=self._on_audio,
                )
                self._mic_stream.start()
        except Exception as exc:
            devs = list_microphones()
            raise RuntimeError(
                f"麦克风打开失败: {exc}\n"
                f"可用输入设备: {devs or '（无）——请检查麦克风是否已插入/启用'}\n"
                f"可在 config.yaml voice.sherpa.mic_device 指定设备序号"
            ) from exc

        logger.info("音频主循环启动（idle: KWS 唤醒监听）")
        try:
            while self._running.is_set():
                try:
                    block = self._q.get(timeout=0.1)
                except queue.Empty:
                    self._check_timeouts()
                    continue
                self._feed(block)
        finally:
            self._close()

    def _start_file_source(self, wav_path: Path) -> None:
        """调试用：把 wav 文件按 30ms 块灌进队列（代替麦克风）。"""

        def job() -> None:
            import wave

            with wave.open(str(wav_path), "rb") as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if sr != SAMPLE_RATE:
                # 线性重采样到 16k
                n_out = int(len(samples) * SAMPLE_RATE / sr)
                samples = np.interp(
                    np.linspace(0, len(samples) - 1, n_out), np.arange(len(samples)), samples
                ).astype(np.float32)
            block = np.zeros(BLOCK_SIZE, dtype=np.float32)
            for i in range(0, len(samples), BLOCK_SIZE):
                if not self._running.is_set():
                    return
                chunk = samples[i : i + BLOCK_SIZE]
                block[: len(chunk)] = chunk
                # 直接入队（文件源不走麦克风回调）
                try:
                    self._q.put_nowait(block[: len(chunk)].copy())
                except queue.Full:
                    pass
                time.sleep(BLOCK_SIZE / SAMPLE_RATE)

        threading.Thread(target=job, daemon=True).start()

    def _feed(self, block: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(np.square(block))))
        self._level = max(rms, self._level * 0.82)
        if self._state == "idle":
            self._ring.append(block)
            self.kws.accept(self._kws_stream, block)
            hit = self.kws.decode(self._kws_stream)
            if not hit and self.idle_asr_enabled:
                hit = self._idle_asr_check(block)
            if hit:
                logger.info("KWS 命中: %s", hit)
                self._enter_listening()
            return

        # listening
        self._ring.append(block)
        if self._processing.is_set():
            # 命令执行中：只入环形缓冲，等执行完回放
            return
        assert self._asr_stream is not None
        self.asr.accept(self._asr_stream, block)
        self.asr.decode(self._asr_stream)
        self._utt_chunks.append(block.copy())

        partial = self.asr.partial(self._asr_stream)
        now = time.time()
        if partial and partial != self._last_partial:
            if now - self._last_emit >= PARTIAL_EMIT_INTERVAL:
                self._last_partial = partial
                self._last_emit = now
                self.assistant.emit_transcript(partial)
            self._phrase_deadline = now + PHRASE_TIMEOUT

        if self.asr.is_endpoint(self._asr_stream):
            text = self.asr.finalize(self._asr_stream).strip()
            self._last_partial = ""
            if text:
                # SenseVoice 终句精修：整句重识别补标点+数字规范化（CPU ~0.3s）
                utterance = None
                if self._utt_chunks:
                    utterance = np.concatenate(self._utt_chunks)
                if self._refiner is None and utterance is not None and len(utterance) > SAMPLE_RATE:
                    try:
                        from screen_agent.voice.sherpa_engine import SenseVoiceRefiner

                        sv_dir = Path(asr_model_dir).parent / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
                        if sv_dir.is_dir():
                            self._refiner = SenseVoiceRefiner(sv_dir)
                    except Exception:
                        self._refiner = None
                if self._refiner is not None and utterance is not None:
                    try:
                        refined = self._refiner.refine(utterance)
                        if refined and len(refined) >= len(text) * 0.8:
                            text = refined
                    except Exception:
                        pass
                self.assistant.emit_transcript(text)
                self._dispatch_text(text)
            else:
                self._refresh_stream()

    def _idle_asr_check(self, block: np.ndarray) -> str:
        """IDLE 态叠加 ASR 转写，支持英文唤醒词（Retina）文本匹配。"""
        if self._asr_stream is None:
            self._asr_stream = self.asr.create_stream()
        self.asr.accept(self._asr_stream, block)
        self.asr.decode(self._asr_stream)
        self._utt_chunks.append(block.copy())
        text = self.asr.partial(self._asr_stream)
        if text and self.assistant.contains_wake_word(text):
            stream = self._asr_stream
            self._asr_stream = None
            final = self.asr.finalize(stream)
            return final or text
        # 防止无限增长：定期重置（每 ~10s 无命中就重建流）
        return ""

    def _enter_listening(self) -> None:
        self._state = "listening"
        self._listening_started = time.time()
        self._phrase_deadline = time.time() + PHRASE_TIMEOUT
        self._refresh_stream(replay_ring=True)
        self.assistant.set_status("listening")

    def _refresh_stream(self, replay_ring: bool = False) -> None:
        self._utt_chunks.clear()
        self._asr_stream = self.asr.create_stream()
        self._last_partial = ""
        if replay_ring:
            for old in list(self._ring):
                self.asr.accept(self._asr_stream, old)
            self.asr.decode(self._asr_stream)

    def _dispatch_text(self, text: str) -> None:
        if self.assistant.in_session:
            self._phrase_deadline = 0.0
            self._cmd_q.put(text)
            return

        # 唤醒词命中后的第一句
        command = self.assistant.strip_wake_word(text)
        if not command:
            greeting = self.assistant.begin_wake_session()
            if greeting:
                self.assistant.emit_result(greeting)
                self.assistant.speak(greeting)
            if not self.assistant.session_enabled:
                self._state = "idle"
                self.assistant.set_status("idle")
            else:
                self._refresh_stream()
                self._phrase_deadline = time.time() + PHRASE_TIMEOUT
            return

        self._phrase_deadline = 0.0
        self._cmd_q.put(command)

    def _check_timeouts(self) -> None:
        if self._state != "listening" or self._processing.is_set():
            return
        now = time.time()

        # 会话过期 → 回 idle
        if self.assistant.session_expired:
            logger.info("会话超时，回到待唤醒状态")
            self.assistant.end_session()
            self._state = "idle"
            self.assistant.set_status("idle")
            return

        if self._phrase_deadline and now > self._phrase_deadline:
            assert self._asr_stream is not None
            text = self.asr.finalize(self._asr_stream).strip()
            self._last_partial = ""
            if text:
                self.assistant.emit_transcript(text)
                self._dispatch_text(text)
                return
            if now - self._listening_started > LISTENING_MAX or not self.assistant.in_session:
                logger.info("监听超时无语音")
                if self.assistant.in_session:
                    self._refresh_stream()
                    self._phrase_deadline = now + PHRASE_TIMEOUT
                else:
                    self._state = "idle"
                    self.assistant.set_status("idle")

    # ---- 生命周期 ----

    def stop(self) -> None:
        self._running.clear()

    def _close(self) -> None:
        self._running.clear()
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                logger.warning("关闭麦克风流失败", exc_info=True)
            self._mic_stream = None
        if self._worker is not None:
            self._cmd_q.put(None)
            self._worker.join(timeout=3)
            self._worker = None
        logger.info("音频主循环已退出")
