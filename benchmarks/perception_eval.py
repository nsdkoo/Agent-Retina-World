"""感知层评测：活动分类 + 隐私闸门 + 数据飞轮 + 自进化。

用法：
    python benchmarks/perception_eval.py              # 跑评测并与基线对比
    python benchmarks/perception_eval.py --save       # 把本次结果存成新基线
    python benchmarks/perception_eval.py --evolve     # 跑一轮自进化
    python benchmarks/perception_eval.py --flywheel   # 看飞轮里攒了哪些候选

三件事分开看：
- **评测**回答「现在多好」，靠黄金集和基线，每次改动都该跑
- **飞轮**回答「接下来该看什么」，靠真实使用里挖出的难例
- **自进化**回答「能不能自己变好」，靠生成候选 + 留出集验证
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from screen_agent.capture.privacy import PrivacyGate  # noqa: E402
from screen_agent.eval import GoldenSet, Evaluator, compare_to_baseline  # noqa: E402
from screen_agent.eval.flywheel import Flywheel  # noqa: E402
from screen_agent.evolve import Archive, Evolver  # noqa: E402
from screen_agent.understand.classify import ActivityClassifier  # noqa: E402

DATA_DIR = ROOT / "data" / "eval"
GOLDEN_PATH = DATA_DIR / "golden.json"
BASELINE_PATH = DATA_DIR / "baseline.json"
FLYWHEEL_PATH = DATA_DIR / "flywheel.json"
ARCHIVE_PATH = DATA_DIR / "archive.json"


def build(args: argparse.Namespace) -> tuple[Evaluator, GoldenSet]:
    classifier = ActivityClassifier(
        service_url="" if args.no_service else "http://127.0.0.1:8790",
        enabled=True,
    )
    privacy = PrivacyGate()
    golden = GoldenSet.load_or_seed(GOLDEN_PATH)
    evaluator = Evaluator(classifier=classifier, privacy=privacy, system_version="perception-1.3")
    return evaluator, golden


def cmd_eval(args: argparse.Namespace) -> int:
    evaluator, golden = build(args)
    report = evaluator.run(golden)
    print()
    print("=" * 68)
    print(report.summary())
    print("=" * 68)

    baseline = {}
    if BASELINE_PATH.exists():
        import json

        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    passed, reasons = compare_to_baseline(report, baseline)
    print()
    for reason in reasons:
        print(f"  {'✓' if passed else '✗'} {reason}")

    if not baseline:
        print("\n  （首次运行，建议加 --save 把它钉成基线）")
    if args.save:
        report.save(BASELINE_PATH)
        golden.save(GOLDEN_PATH)
        print(f"\n  已存基线 → {BASELINE_PATH}")
        print(f"  已存黄金集 → {GOLDEN_PATH}")
    return 0 if passed else 1


def cmd_flywheel(args: argparse.Namespace) -> int:
    flywheel = Flywheel(FLYWHEEL_PATH)
    stats = flywheel.stats()
    print(f"\n候选池：{stats['pending']} 条待看，其中 {stats['confirmed']} 条已确认")
    print(f"来源分布：{stats['by_source'] or '（空）'}")
    if stats["top_repeated"]:
        print("\n反复出现的场景（这些最值得看）：")
        for row in stats["top_repeated"]:
            print(f"  · {row['window'][:30]:<32} 判成 {row['guess']:<10} 见到 {row['seen']} 次")
    confirmed = flywheel.pending(only_confirmed=True)
    if confirmed:
        golden = GoldenSet.load_or_seed(GOLDEN_PATH)
        added = flywheel.promote_confirmed(golden)
        golden.save(GOLDEN_PATH)
        print(f"\n已把 {len(added)} 条确认过的候选晋升进黄金集：")
        for case in added:
            print(f"  · {case.case_id}: {case.window_title[:28]} → {case.expect}")
    return 0


def cmd_evolve(args: argparse.Namespace) -> int:
    evaluator, golden = build(args)
    print("\n[1/3] 基线评测")
    base = evaluator.run(golden)
    print("  " + base.activity.summary("活动分类：").replace("\n", "\n  "))

    failures = base.activity.errors
    if not failures:
        print("\n  开发集上没有错判，没什么可进化的。")
        return 0

    print(f"\n[2/3] 从 {len(failures)} 个错判里生成候选改进")
    evolver = Evolver(evaluator, golden, Archive(ARCHIVE_PATH))
    results = evolver.step(failures)

    print(f"\n[3/3] 留出集验证（候选看不到这一部分，防奖励黑客）")
    for variant in results:
        print("  " + variant.summary())

    accepted = [v for v in results if v.accepted]
    print(f"\n  采纳 {len(accepted)}/{len(results)} 项。")
    if accepted:
        print("  注意：这里只**验证**了改进有效，落地要人工把关键词并进 classify.py 的 _RULES。")
        print("  档案库记录了血缘，出问题可以一路回查。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="感知层评测与自进化")
    parser.add_argument("--save", action="store_true", help="把本次结果存成基线")
    parser.add_argument("--evolve", action="store_true", help="跑一轮自进化")
    parser.add_argument("--flywheel", action="store_true", help="查看飞轮候选池")
    parser.add_argument("--no-service", action="store_true", help="只用规则，不连本地模型服务")
    args = parser.parse_args(argv)

    if args.flywheel:
        return cmd_flywheel(args)
    if args.evolve:
        return cmd_evolve(args)
    return cmd_eval(args)


if __name__ == "__main__":
    raise SystemExit(main())
