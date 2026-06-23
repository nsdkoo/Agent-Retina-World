from __future__ import annotations

import json
import logging
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

VOSK_MODEL_URL = (
    "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
)
VOSK_MODEL_DIR = "vosk-model-small-cn-0.22"


class STTEngine(ABC):
    @abstractmethod
    def transcribe(self, audio_data, sample_rate: int) -> str | None:
        ...


class GoogleSTT(STTEngine):
    def __init__(self, language: str = "zh-CN") -> None:
        self.language = language

    def transcribe(self, audio_data, sample_rate: int) -> str | None:
        import speech_recognition as sr

        recognizer = sr.Recognizer()
        try:
            text = recognizer.recognize_google(audio_data, language=self.language)
            return text.strip() if text else None
        except sr.UnknownValueError:
            return None
        except Exception as exc:
            logger.warning("Google STT 失败: %s", exc)
            return None


class VoskSTT(STTEngine):
    """离线语音识别（Vosk 中文小模型）。"""

    def __init__(self, model_path: Path) -> None:
        if not model_path.is_dir():
            raise FileNotFoundError(
                f"Vosk 模型不存在: {model_path}\n"
                f"运行: python main.py voice --download-model"
            )
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        self._model = Model(str(model_path))

    def transcribe(self, audio_data, sample_rate: int) -> str | None:
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self._model, sample_rate)
        raw = audio_data.get_wav_data(convert_rate=sample_rate, convert_width=2)
        rec.AcceptWaveform(raw)
        result = json.loads(rec.FinalResult())
        text = result.get("text", "").strip()
        return text or None


def resolve_stt_engine(engine_name: str, model_path: Path, language: str) -> STTEngine:
    if engine_name == "google":
        return GoogleSTT(language=language)
    if engine_name == "vosk":
        return VoskSTT(model_path)
    if engine_name == "auto":
        try:
            return VoskSTT(model_path)
        except FileNotFoundError:
            logger.info("Vosk 模型未找到，回退在线 Google STT")
            return GoogleSTT(language=language)
    raise ValueError(f"未知 STT 引擎: {engine_name}")


def download_vosk_model(dest_root: Path) -> Path:
    dest_root.mkdir(parents=True, exist_ok=True)
    target = dest_root / VOSK_MODEL_DIR
    if target.is_dir():
        return target

    zip_path = dest_root / "vosk-model-small-cn-0.22.zip"
    print(f"正在下载离线语音模型 → {zip_path}")
    with httpx.stream("GET", VOSK_MODEL_URL, follow_redirects=True, timeout=600) as resp:
        resp.raise_for_status()
        with zip_path.open("wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)

    print("解压中…")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_root)
    zip_path.unlink(missing_ok=True)
    print(f"模型就绪: {target}")
    return target
