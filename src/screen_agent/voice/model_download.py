"""sherpa-onnx 模型套件下载器（走 ghfast.top 镜像，失败重试 + 半截文件清理）。"""

from __future__ import annotations

import logging
import tarfile
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MIRROR = "https://ghfast.top/"

SHERPA_MODELS: list[dict] = [
    {
        "key": "asr",
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20.tar.bz2"
        ),
        "dir": "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
    },
    {
        "key": "kws",
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
            "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
        ),
        "dir": "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
    },
    {
        "key": "tts",
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
            "vits-zh-hf-fanchen-C.tar.bz2"
        ),
        "dir": "vits-zh-hf-fanchen-C",
    },
]


def _download(url: str, dest: Path, attempts: int = 3) -> None:
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            with httpx.stream(
                "GET", url, follow_redirects=True, timeout=600
            ) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                done = 0
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes(1024 * 256):
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            print(f"\r  下载 {pct}% ({done // 1048576}MB/{total // 1048576}MB)", end="")
                print()
                return
        except Exception as exc:
            last_exc = exc
            logger.warning("下载失败（第 %d 次）: %s", i, exc)
            dest.unlink(missing_ok=True)
    raise RuntimeError(f"下载失败: {url}") from last_exc


def _extract(tar_path: Path, dest_root: Path) -> None:
    print(f"解压 {tar_path.name} …")
    with tarfile.open(tar_path, "r:bz2") as tf:
        tf.extractall(dest_root)
    tar_path.unlink(missing_ok=True)


def ensure_sherpa_models(models_root: Path) -> dict[str, Path]:
    """确保三个模型目录就绪，返回 {key: model_dir}。"""
    models_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for item in SHERPA_MODELS:
        target = models_root / item["dir"]
        if target.is_dir() and any(target.iterdir()):
            result[item["key"]] = target
            print(f"[{item['key']}] 已就绪: {target.name}")
            continue
        print(f"[{item['key']}] 开始下载: {item['dir']}")
        archive = models_root / f"{item['key']}.tar.bz2"
        _download(MIRROR + item["url"], archive)
        _extract(archive, models_root)
        if not target.is_dir():
            raise RuntimeError(f"解压后未找到目录 {target}")
        result[item["key"]] = target
        print(f"[{item['key']}] 完成")
    return result
