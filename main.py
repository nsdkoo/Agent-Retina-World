#!/usr/bin/env python3
"""屏幕世界感知 Agent CLI"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

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
    print(json.dumps(pipeline.stats.__dict__, ensure_ascii=False, indent=2))


def cmd_watch(args: argparse.Namespace) -> None:
    pipeline = PerceptionPipeline.from_config(args.config)
    interval = args.interval
    print(f"开始监听，间隔 {interval}s，Ctrl+C 停止")
    try:
        while True:
            result = pipeline.run_once()
            print(json.dumps(result, ensure_ascii=False))
            time.sleep(interval)
    except KeyboardInterrupt:
        n = pipeline.flush()
        print(f"\n已停止，收尾事件 {n} 条")
        print(json.dumps(pipeline.stats.__dict__, ensure_ascii=False, indent=2))


def cmd_report(args: argparse.Namespace) -> None:
    pipeline = PerceptionPipeline.from_config(args.config)
    summary = pipeline.proactive.daily_summary()
    todos = pipeline.proactive.todo_suggestions()
    print(summary)
    print("\n## 待办建议\n")
    for t in todos:
        print(f"- {t}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent-Retina-World · 屏幕世界感知 Agent v0.1")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config.yaml",
        help="配置文件路径",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_once = sub.add_parser("once", help="采集并理解一帧")
    p_once.set_defaults(func=cmd_once)

    p_watch = sub.add_parser("watch", help="定时采集")
    p_watch.add_argument("--interval", type=int, default=30)
    p_watch.set_defaults(func=cmd_watch)

    p_report = sub.add_parser("report", help="生成每日总结")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.config = ensure_config(args.config)
    args.func(args)


if __name__ == "__main__":
    main()
