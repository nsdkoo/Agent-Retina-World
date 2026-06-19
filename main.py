#!/usr/bin/env python3
"""Agent-Retina-World CLI"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from screen_agent import __version__
from screen_agent.pipeline import PerceptionPipeline  # noqa: E402


def ensure_config(path: Path) -> Path:
    if path.exists():
        return path
    example = ROOT / "config.example.yaml"
    if example.exists():
        shutil.copy(example, path)
        print(f"已生成默认配置: {path}")
    return path


def cmd_once(args: argparse.Namespace) -> None:
    pipeline = PerceptionPipeline.from_config(args.config)
    result = pipeline.run_once()
    pipeline.flush()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n--- stats ---")
    print(json.dumps(pipeline.runtime_stats(), ensure_ascii=False, indent=2))


def cmd_watch(args: argparse.Namespace) -> None:
    pipeline = PerceptionPipeline.from_config(args.config)
    interval = args.interval
    print(f"Agent-Retina-World v{__version__} · 间隔 {interval}s · Ctrl+C 停止")
    try:
        while True:
            result = pipeline.run_once()
            print(json.dumps(result, ensure_ascii=False))
            time.sleep(interval)
    except KeyboardInterrupt:
        n = pipeline.flush()
        print(f"\n已停止，收尾事件 {n} 条")
        print(json.dumps(pipeline.runtime_stats(), ensure_ascii=False, indent=2))


def cmd_report(args: argparse.Namespace) -> None:
    pipeline = PerceptionPipeline.from_config(args.config)
    summary = pipeline.proactive.daily_summary()
    todos = pipeline.proactive.todo_suggestions()
    print(summary)
    print("\n## 待办建议\n")
    for t in todos:
        print(f"- {t}")


def cmd_timeline(args: argparse.Namespace) -> None:
    pipeline = PerceptionPipeline.from_config(args.config)
    print(pipeline.proactive.timeline_markdown(limit=args.limit))


def cmd_stats(args: argparse.Namespace) -> None:
    pipeline = PerceptionPipeline.from_config(args.config)
    print(json.dumps(pipeline.runtime_stats(), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Agent-Retina-World · 屏幕世界感知 Agent v{__version__}"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("once", help="采集并理解一帧").set_defaults(func=cmd_once)

    p_watch = sub.add_parser("watch", help="定时采集")
    p_watch.add_argument("--interval", "-i", type=int, default=30)
    p_watch.set_defaults(func=cmd_watch)

    sub.add_parser("report", help="生成每日总结").set_defaults(func=cmd_report)

    p_tl = sub.add_parser("timeline", help="查看活动时间线")
    p_tl.add_argument("--limit", type=int, default=20)
    p_tl.set_defaults(func=cmd_timeline)

    sub.add_parser("stats", help="查看运行统计").set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.config = ensure_config(args.config)
    args.func(args)


if __name__ == "__main__":
    main()
