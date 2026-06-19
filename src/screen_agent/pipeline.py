from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from screen_agent.activity.store import ActivityAggregator, MemoryStore
from screen_agent.capture.screen import ScreenCapturer
from screen_agent.dedup.hasher import ScreenshotDeduper
from screen_agent.proactive.service import ProactiveService
from screen_agent.understand.vlm import HeuristicAnalyzer, OpenAICompatibleVLMAnalyzer, VLMAnalyzer


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
    deduper: ScreenshotDeduper
    analyzer: VLMAnalyzer
    aggregator: ActivityAggregator
    memory: MemoryStore
    proactive: ProactiveService
    stats: PipelineStats = field(default_factory=PipelineStats)

    @classmethod
    def from_config(cls, config_path: Path) -> PerceptionPipeline:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        capture_cfg = raw["capture"]
        dedup_cfg = raw["dedup"]
        vlm_cfg = raw["vlm"]
        memory_cfg = raw["memory"]
        proactive_cfg = raw["proactive"]
        activity_cfg = raw.get("activity", {})

        capturer = ScreenCapturer(Path(capture_cfg["output_dir"]))
        deduper = ScreenshotDeduper(
            phash_threshold=int(dedup_cfg["phash_threshold"]),
            histogram_threshold=float(dedup_cfg.get("histogram_threshold", 0.98)),
            enable_histogram=bool(dedup_cfg.get("enable_histogram", True)),
        )
        memory = MemoryStore(Path(memory_cfg["db_path"]))
        proactive = ProactiveService(memory, Path(proactive_cfg["output_dir"]))
        aggregator = ActivityAggregator(
            merge_window=__import__("datetime").timedelta(
                minutes=int(activity_cfg.get("merge_window_minutes", 3))
            )
        )

        provider = vlm_cfg.get("provider", "heuristic")
        if provider == "openai_compatible":
            analyzer: VLMAnalyzer = OpenAICompatibleVLMAnalyzer(
                base_url=vlm_cfg["base_url"],
                model=vlm_cfg["model"],
                api_key=vlm_cfg.get("api_key", ""),
            )
        else:
            analyzer = HeuristicAnalyzer()

        return cls(
            capturer=capturer,
            deduper=deduper,
            analyzer=analyzer,
            aggregator=aggregator,
            memory=memory,
            proactive=proactive,
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
