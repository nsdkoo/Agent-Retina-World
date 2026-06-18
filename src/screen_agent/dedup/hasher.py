from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image


@dataclass
class DedupResult:
    is_duplicate: bool
    reason: str | None = None
    phash_distance: int | None = None


class ScreenshotDeduper:
    """多阶段截图去重：感知哈希 + 预留语义相似度接口。"""

    def __init__(self, phash_threshold: int = 8) -> None:
        self.phash_threshold = phash_threshold
        self._recent_hashes: list[tuple[str, imagehash.ImageHash]] = []
        self._max_history = 200

    def _phash(self, path: Path) -> imagehash.ImageHash:
        with Image.open(path) as img:
            return imagehash.phash(img)

    def check(self, path: Path) -> DedupResult:
        current = self._phash(path)
        for _, prev in self._recent_hashes:
            distance = current - prev
            if distance <= self.phash_threshold:
                return DedupResult(
                    is_duplicate=True,
                    reason="phash",
                    phash_distance=distance,
                )

        self._recent_hashes.append((str(path), current))
        if len(self._recent_hashes) > self._max_history:
            self._recent_hashes.pop(0)

        return DedupResult(is_duplicate=False)

    @property
    def stats(self) -> dict[str, int]:
        return {"history_size": len(self._recent_hashes)}
