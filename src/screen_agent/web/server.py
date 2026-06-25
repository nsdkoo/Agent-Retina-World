from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from screen_agent.config import load_yaml
from screen_agent.pipeline import PerceptionPipeline

WEB_ROOT = Path(__file__).resolve().parents[3] / "web"
STATIC_DIR = WEB_ROOT / "static"


def create_app(config_path: Path) -> FastAPI:
    pipeline = PerceptionPipeline.from_config(config_path)
    app = FastAPI(title="Agent-Retina-World", version="0.3.0")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "project": "Agent-Retina-World"}

    @app.get("/api/events")
    def events(limit: int = 50) -> dict:
        items = pipeline.memory.list_events(limit=limit)
        return {
            "events": [
                {
                    "event_id": e.event_id,
                    "started_at": e.started_at.isoformat(),
                    "ended_at": e.ended_at.isoformat(),
                    "page_category": e.page_category,
                    "user_action": e.user_action,
                    "summary": e.summary,
                    "frame_count": e.frame_count,
                    "evidence_count": len(e.evidence_paths),
                    "task_tag": e.task_tag,
                    "duration_minutes": round(e.duration_seconds() / 60, 2),
                }
                for e in items
            ]
        }

    @app.get("/api/stats")
    def stats() -> dict:
        return pipeline.runtime_stats()

    @app.get("/api/timeline")
    def timeline(limit: int = 30) -> dict:
        return {"markdown": pipeline.proactive.timeline_markdown(limit=limit)}

    @app.get("/api/categories")
    def categories() -> dict:
        totals = pipeline.memory.time_by_category()
        return {
            "categories": [
                {"name": k, "minutes": round(v / 60, 2)}
                for k, v in sorted(totals.items(), key=lambda x: -x[1])
            ]
        }

    return app


def run_server(config_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    app = create_app(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")
