from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import imagehash
from PIL import Image


@dataclass
class DedupResult:
    is_duplicate: bool
    reason: str | None = None
    phash_distance: int | None = None
    histogram_distance: float | None = None


@dataclass
class DedupStats:
    total_checks: int = 0
    phash_hits: int = 0
    histogram_hits: int = 0
    passed: int = 0

    @property
    def skip_rate(self) -> float:
        if self.total_checks == 0:
            return 0.0
        skipped = self.phash_hits + self.histogram_hits
        return skipped / self.total_checks

    def to_dict(self) -> dict:
        return {
            "total_checks": self.total_checks,
            "phash_hits": self.phash_hits,
            "histogram_hits": self.histogram_hits,
            "passed": self.passed,
            "skip_rate": round(self.skip_rate, 4),
        }


class ScreenshotDeduper:
    """多阶段截图去重：L1 感知哈希 + L2 直方图相似度。"""

    def __init__(
        self,
        phash_threshold: int = 8,
        histogram_threshold: float = 0.98,
        enable_histogram: bool = True,
    ) -> None:
        self.phash_threshold = phash_threshold
        self.histogram_threshold = histogram_threshold
        self.enable_histogram = enable_histogram
        self._recent: list[tuple[str, imagehash.ImageHash, Image.Image]] = []
        self._max_history = 200
        self.stats = DedupStats()

    def _load_image(self, path: Path) -> Image.Image:
        with Image.open(path) as img:
            return img.convert("RGB").resize((320, 180))

    def _phash(self, img: Image.Image) -> imagehash.ImageHash:
        return imagehash.phash(img)

    def _histogram_similarity(self, a: Image.Image, b: Image.Image) -> float:
        """缩略图 RGB 直方图余弦相似度，用于 L2 去重。"""
        ha = a.histogram()
        hb = b.histogram()
        dot = sum(x * y for x, y in zip(ha, hb, strict=False))
        na = sum(x * x for x in ha) ** 0.5
        nb = sum(y * y for y in hb) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def check(self, path: Path) -> DedupResult:
        self.stats.total_checks += 1
        img = self._load_image(path)
        current_hash = self._phash(img)

        for _, prev_hash, prev_img in self._recent:
            distance = current_hash - prev_hash
            if distance <= self.phash_threshold:
                self.stats.phash_hits += 1
                return DedupResult(
                    is_duplicate=True,
                    reason="phash",
                    phash_distance=distance,
                )

            if self.enable_histogram:
                sim = self._histogram_similarity(img, prev_img)
                if sim >= self.histogram_threshold:
                    self.stats.histogram_hits += 1
                    return DedupResult(
                        is_duplicate=True,
                        reason="histogram",
                        histogram_distance=sim,
                    )

        self._recent.append((str(path), current_hash, img))
        if len(self._recent) > self._max_history:
            self._recent.pop(0)

        self.stats.passed += 1
        return DedupResult(is_duplicate=False)
