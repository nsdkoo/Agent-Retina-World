"""AudioLoop 全链路验证（无麦克风）：TTS 合成唤醒词 → 文件源灌入 → KWS 命中 → 状态流转。

运行: .venv/Scripts/python.exe scripts/e2e_audio_loop.py
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from screen_agent.voice.executor import ActionResult  # noqa: E402
from screen_agent.voice.sherpa_engine import SherpaTts  # noqa: E402

TTS_DIR = ROOT / "models" / "vits-zh-hf-fanchen-C"


class StubAssistant:
    """AudioLoop 需要的助手接口桩。"""

    def __init__(self) -> None:
        self.wake_names = ["小光", "光光"]
        self.session_enabled = True
        self._in_session = False
        self.events: list[str] = []
        self.status = ""

    @property
    def in_session(self) -> bool:
        return self._in_session

    @property
    def session_expired(self) -> bool:
        return False

    def set_status(self, status: str) -> None:
        self.status = status
        self.events.append(f"status:{status}")
        print(f"  [status] {status}")

    def emit_transcript(self, text: str) -> None:
        self.events.append(f"transcript:{text}")
        print(f"  [transcript] {text}")

    def emit_result(self, text: str) -> None:
        self.events.append(f"result:{text}")
        print(f"  [result] {text}")

    def speak(self, text: str) -> None:
        print(f"  [tts] {text}")

    def contains_wake_word(self, text: str) -> bool:
        return any(w in text for w in self.wake_names)

    def strip_wake_word(self, text: str) -> str:
        for w in self.wake_names:
            text = text.replace(w, "")
        return text.strip(" ，,")

    def begin_wake_session(self) -> str:
        self._in_session = True
        self.events.append("wake")
        print("  [wake] 唤醒成功，进入连续对话")
        return "我在，请说你要做什么"

    def handle_command(self, command: str) -> ActionResult:
        self.events.append(f"command:{command}")
        print(f"  [command] {command}")
        return ActionResult(success=True, message=f"已执行：{command}")


def synth_wav(text: str, out_path: Path, tail_silence: float = 1.2) -> None:
    tts = SherpaTts(TTS_DIR)
    samples, sr = tts.synthesize(text)
    silence = np.zeros(int(tail_silence * sr), dtype=np.float32)
    audio = np.concatenate([np.asarray(samples, dtype=np.float32), silence])
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def main() -> int:
    print("== 1. TTS 合成唤醒词音频 ==")
    wav_path = ROOT / "data" / "test_wake_xiaoguang.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    synth_wav("小光", wav_path)
    print(f"音频就绪: {wav_path}")

    print("== 2. 启动 AudioLoop（文件源） ==")
    from screen_agent.voice.audio_loop import AudioLoop

    assistant = StubAssistant()
    loop = AudioLoop(
        assistant,
        asr_model_dir=ROOT / "models" / "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
        kws_model_dir=ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
        keywords_file=ROOT / "data" / "keywords.txt",
        kws_threshold=0.4,
        num_threads=2,
        audio_file=wav_path,
    )
    loop.run_in_background = None  # 不用 assistant 线程
    worker = __import__("threading").Thread(target=loop.run, daemon=True)
    worker.start()

    print("== 3. 等待 KWS 命中（最多 30s） ==")
    deadline = time.time() + 30
    hit = False
    while time.time() < deadline:
        if "wake" in assistant.events or any(e.startswith("status:listening") for e in assistant.events):
            hit = True
            break
        time.sleep(0.3)

    loop.stop()
    time.sleep(1)

    print("\n== 结果 ==")
    print("事件流:", assistant.events)
    if hit:
        print("E2E OK: KWS 唤醒命中")
        return 0
    print("E2E FAIL: 未检测到唤醒")
    return 1


if __name__ == "__main__":
    sys.exit(main())
