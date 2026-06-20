from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from screen_agent.activity.store import ActivityAggregator, MemoryStore
from screen_agent.capture.screen import ScreenCapturer
from screen_agent.config import load_yaml, resolve_secret
from screen_agent.dedup.hasher import ScreenshotDeduper
from screen_agent.dedup.pipeline import MultiStageDeduper
from screen_agent.dedup.semantic import EmbeddingClient, SemanticDeduper
from screen_agent.proactive.service import ProactiveService
from screen_agent.understand.vlm import VLMAnalyzer, build_analyzer


@dataclass
class PipelineStats:
    captured: int = 0
    dedup_skipped: int = 0
    analyzed: int = 0
    events_created: int = 0
    vlm_calls_saved: int = 0

    def to_dict(self) -> dict:
        return {
            "captured": self.captured,
            "dedup_skipped": self.dedup_skipped,
            "analyzed": self.analyzed,
            "events_created": self.events_created,
            "vlm_calls_saved": self.vlm_calls_saved,
            "dedup_skip_rate": round(self.dedup_skipped / self.captured, 4) if self.captured else 0.0,
        }


@dataclass
class PerceptionPipeline:
    capturer: ScreenCapturer
    deduper: MultiStageDeduper
    analyzer: VLMAnalyzer
    aggregator: ActivityAggregator
    memory: MemoryStore
    proactive: ProactiveService
    semantic_post: SemanticDeduper | None = None
    stats: PipelineStats = field(default_factory=PipelineStats)

    @classmethod
    def from_config(cls, config_path: Path) -> PerceptionPipeline:
        raw = load_yaml(config_path)
        capture_cfg = raw["capture"]
        dedup_cfg = raw["dedup"]
        vlm_cfg = raw["vlm"]
        memory_cfg = raw["memory"]
        proactive_cfg = raw["proactive"]
        activity_cfg = raw.get("activity", {})
        embed_cfg = raw.get("embedding", {})

        capturer = ScreenCapturer(Path(capture_cfg["output_dir"]))
        visual = ScreenshotDeduper(
            phash_threshold=int(dedup_cfg["phash_threshold"]),
            histogram_threshold=float(dedup_cfg.get("histogram_threshold", 0.98)),
            enable_histogram=bool(dedup_cfg.get("enable_histogram", True)),
        )

        semantic: SemanticDeduper | None = None
        if embed_cfg.get("enabled"):
            api_key = resolve_secret(embed_cfg.get("api_key"), embed_cfg.get("api_key_env", "EMBEDDING_API_KEY"))
            client = EmbeddingClient(
                base_url=embed_cfg["base_url"],
                model=embed_cfg["model"],
                api_key=api_key,
            )
            semantic = SemanticDeduper(
                client=client,
                threshold=float(embed_cfg.get("threshold", 0.92)),
            )

        deduper = MultiStageDeduper(visual=visual, semantic=semantic)
        memory = MemoryStore(Path(memory_cfg["db_path"]))
        proactive = ProactiveService(memory, Path(proactive_cfg["output_dir"]))
        aggregator = ActivityAggregator(
            merge_window=__import__("datetime").timedelta(
                minutes=int(activity_cfg.get("merge_window_minutes", 3))
            )
        )
        analyzer = build_analyzer(vlm_cfg)

        return cls(
            capturer=capturer,
            deduper=deduper,
            analyzer=analyzer,
            aggregator=aggregator,
            memory=memory,
            proactive=proactive,
            semantic_post=semantic,
        )

    def run_once(self) -> dict:
        frame = self.capturer.capture()
        self.stats.captured += 1

        dedup = self.deduper.check(frame.path)
        if dedup.is_duplicate:
            self.stats.dedup_skipped += 1
            self.stats.vlm_calls_saved += 1
            return {
                "status": "skipped_duplicate",
                "path": str(frame.path),
                "reason": dedup.reason,
                "phash_distance": dedup.phash_distance,
                "histogram_distance": dedup.histogram_distance,
            }

        understanding = self.analyzer.analyze(frame.path)
        self.stats.analyzed += 1

        if self.semantic_post and understanding.summary:
            self.semantic_post.remember(understanding.summary)

        new_events = self.aggregator.ingest(understanding)
        for evt in new_events:
            self.memory.save_event(evt)
            self.stats.events_created += 1

        return {
            "status": "processed",
            "path": str(frame.path),
            "category": understanding.page_category.value,
            "action": understanding.user_action.value,
            "window_title": understanding.window_title,
            "summary": understanding.summary,
            "source": understanding.source,
            "new_events": len(new_events),
        }

    def flush(self) -> int:
        events = self.aggregator.flush_all()
        for evt in events:
            self.memory.save_event(evt)
            self.stats.events_created += 1
        return len(events)

    def runtime_stats(self) -> dict:
        return {
            "pipeline": self.stats.to_dict(),
            "dedup": self.deduper.stats.to_dict(),
            "memory": self.memory.summary_stats(),
        }
