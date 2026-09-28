"""sherpa 引擎冒烟测试：keywords 生成 → KWS/ASR 加载 → 用测试 wav 验证 → TTS 合成。

运行: .venv/Scripts/python.exe scripts/smoke_sherpa.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from screen_agent.voice.sherpa_engine import (  # noqa: E402
    SherpaAsr,
    SherpaKws,
    SherpaTts,
    build_keywords_txt,
)

MODELS = ROOT / "models"
ASR_DIR = MODELS / "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
KWS_DIR = MODELS / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
TTS_DIR = MODELS / "vits-zh-hf-fanchen-C"


def main() -> None:
    import numpy as np

    print("== 1. 生成 keywords.txt ==")
    kw_path = build_keywords_txt(["小光", "光光"], KWS_DIR, ROOT / "data" / "keywords.txt", 0.4)
    print(kw_path.read_text(encoding="utf-8"))

    print("== 2. 加载 ASR ==")
    t0 = time.time()
    asr = SherpaAsr(ASR_DIR, num_threads=2)
    print(f"ASR 就绪 {time.time() - t0:.1f}s")

    print("== 3. ASR 转写测试 wav（0.wav） ==")
    import wave

    wav_path = ASR_DIR / "test_wavs" / "0.wav"
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    stream = asr.create_stream()
    t0 = time.time()
    # 按 30ms 块流式喂入，模拟麦克风
    block = int(0.03 * sr)
    for i in range(0, len(samples), block):
        chunk = samples[i : i + block]
        asr.accept(stream, chunk)
        asr.decode(stream)
    text = asr.finalize(stream)
    print(f"转写({time.time() - t0:.2f}s, {len(samples)/sr:.1f}s 音频): {text}")

    print("== 4. 加载 KWS ==")
    t0 = time.time()
    kws = SherpaKws(KWS_DIR, kw_path, threshold=0.4, num_threads=2)
    print(f"KWS 就绪 {time.time() - t0:.1f}s")

    print("== 5. KWS 空音频跑通（不应命中） ==")
    ks = kws.create_stream()
    tail = np.zeros(int(1.0 * 16000), dtype=np.float32)
    kws.accept(ks, tail)
    hit = kws.decode(ks)
    print(f"空音频命中: {hit!r}（应为空）")

    print("== 6. 加载 TTS 并合成 ==")
    t0 = time.time()
    tts = SherpaTts(TTS_DIR)
    print(f"TTS 就绪 {time.time() - t0:.1f}s")
    t0 = time.time()
    samples_out, sr_out = tts.synthesize("你好，我是小光，语音助手已就绪")
    dur = len(samples_out) / sr_out
    print(f"合成 {time.time() - t0:.2f}s → {dur:.1f}s 音频 @ {sr_out}Hz")

    print("\nSMOKE OK")


if __name__ == "__main__":
    main()
