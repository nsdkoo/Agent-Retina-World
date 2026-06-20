from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image

from screen_agent.capture.context import get_foreground_context
from screen_agent.dedup.hasher import DedupResult, DedupStats, ScreenshotDeduper
from screen_agent.dedup.semantic import SemanticDeduper


@dataclass
class CombinedDedupStats:
    visual: DedupStats
    semantic: dict | None = None

    def to_dict(self) -> dict:
        out = {"visual": self.visual.to_dict()}
        if self.semantic is not None:
            out["semantic"] = self.semantic
        total = self.visual.total_checks
        skipped = self.visual.phash_hits + self.visual.histogram_hits
        if self.semantic:
            skipped += self.semantic.get("hits", 0)
        out["combined_skip_rate"] = round(skipped / total, 4) if total else 0.0
        return out


class MultiStageDeduper:
    """L1 pHash + L2 直方图 + L3 embedding 语义去重。"""

    def __init__(self, visual: ScreenshotDeduper, semantic: SemanticDeduper | None = None) -> None:
        self.visual = visual
        self.semantic = semantic

    def _fingerprint(self, path: Path, img: Image.Image) -> str:
        ctx = get_foreground_context()
        ph = str(imagehash.phash(img))
        title = ctx.window_title if ctx else ""
        process = ctx.process_name or "" if ctx else ""
        return f"title:{title}|process:{process}|phash:{ph}|file:{path.name}"

    def check(self, path: Path) -> DedupResult:
        img = self.visual._load_image(path)
        self.visual.stats.total_checks += 1
        current_hash = self.visual._phash(img)

        for _, prev_hash, prev_img in self.visual._recent:
            distance = current_hash - prev_hash
            if distance <= self.visual.phash_threshold:
                self.visual.stats.phash_hits += 1
                return DedupResult(is_duplicate=True, reason="phash", phash_distance=distance)

            if self.visual.enable_histogram:
                sim = self.visual._histogram_similarity(img, prev_img)
                if sim >= self.visual.histogram_threshold:
                    self.visual.stats.histogram_hits += 1
                    return DedupResult(
                        is_duplicate=True,
                        reason="histogram",
                        histogram_distance=sim,
                    )

        if self.semantic:
            fp = self._fingerprint(path, img)
            sem = self.semantic.check(fp)
            if sem.is_duplicate:
                return DedupResult(
                    is_duplicate=True,
                    reason="semantic",
                    histogram_distance=sem.similarity,
                )

        self.visual._recent.append((str(path), current_hash, img))
        if len(self.visual._recent) > self.visual._max_history:
            self.visual._recent.pop(0)
        self.visual.stats.passed += 1
        return DedupResult(is_duplicate=False)

    @property
    def stats(self) -> CombinedDedupStats:
        sem = self.semantic.stats.to_dict() if self.semantic else None
        return CombinedDedupStats(visual=self.visual.stats, semantic=sem)
