#!/usr/bin/env python
"""把各 AI 工具的 Plan 模式产物自动归档进 `docs/plans/`。

## 为什么需要它

项目从 v0.6 起有一条约定：「Plan 模式做的文档都要记在 docs 里，要有过程」。
但它是**人工搬运**的——从 `~/.cursor/plans/` 复制过来，再手写索引。
靠记性的事必然断：最后一次归档是 2026-06-25，之后三个月里各工具的 plan
全散落在各自目录里，一份没进仓库。

这个脚本把它变成自动的：扫各工具的 plans 目录 → 按内容认领本项目的
→ 复制进来 → 维护索引。**幂等**，重复跑不会产生重复文件。

## 用法

    python scripts/archive_plans.py             # 归档（已归档的自动跳过）
    python scripts/archive_plans.py --dry-run   # 只看会归档哪些，不动文件
    python scripts/archive_plans.py --list      # 看各工具 plans 目录现状
    python scripts/archive_plans.py --install-hook   # 装 git pre-commit 钩子

装了钩子之后，每次 `git commit` 都会先跑一遍归档——提交即留档，
不需要记得手动执行。

## 怎么判断一个 plan 属于本项目

看文件内容里有没有出现本项目的标识（目录名 / 仓库路径 / 历史名）。
实测可行：WorkBuddy 生成的 plan 会在正文里写到项目路径。

## 复用到其他项目

直接把这个文件复制到别的项目的 `scripts/` 下即可，**零配置**：
项目根（往上找 `.git`）的名字就是识别标记，各工具的 plans 目录从 `$HOME` 推。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 各 AI 工具的 Plan 模式产物落点。多写几个不存在的也无妨，扫描时跳过
_TOOL_PLAN_DIRS = (
    (".workbuddy", Path.home() / ".workbuddy" / "plans"),
    (".cursor", Path.home() / ".cursor" / "plans"),
    (".claude", Path.home() / ".claude" / "plans"),
    (".codebuddy", Path.home() / ".codebuddy" / "plans"),
    (".codex", Path.home() / ".codex" / "plans"),
)

# 归档登记表：记已收过哪些（按内容 hash），避免重复搬运
_REGISTRY_NAME = ".archive-registry.json"

# 项目历史名——改过名的项目，旧 plan 里写的是旧名，也得能认出来
_HISTORICAL_MARKERS = ("Agent-Retina-World", "Agent-Retina", "D:\\Agent-Retina")


def find_repo_root(start: Path) -> Path | None:
    """从脚本位置往上找 `.git`，定位项目根。"""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def auto_code_markers(repo_root: Path) -> list[str]:
    """自动提取项目特有的**代码路径**作为识别信号。

    为什么需要它：AI 写计划时**更愿意写相对代码路径**（`src/screen_agent/tools/`），
    而不是项目绝对路径。实测有三份本项目的 plan 因为正文里没写 `D:\\素材存储\\...`
    而漏掉，但通篇都在提 `src/screen_agent/`。

    包名是项目特有的，所以这条路比「找绝对路径」可靠得多。
    **自动扫 `src/` 下的包名**，不需要用户手配词表——配了也会过期。
    """
    markers: set[str] = set()
    for parent in ("src", "lib", "app", "packages"):
        base = repo_root / parent
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            # 带父目录的写法最可靠（"src/screen_agent" 很难巧合）
            markers.add(f"{parent}/{child.name}")
    return sorted(markers)


def project_markers(repo_root: Path) -> list[str]:
    """本项目的识别标记。

    目录名是最稳的（plan 正文里写项目路径时必然包含它）；
    再加完整路径的两种斜杠写法、历史名，以及自动提取的代码路径。
    """
    name = repo_root.name
    markers = {
        name,
        name.replace("-", "_"),
        str(repo_root),
        str(repo_root).replace("\\", "/"),
        str(repo_root).replace("/", "\\"),
    }
    markers.update(_HISTORICAL_MARKERS)
    markers.update(auto_code_markers(repo_root))
    return sorted(m for m in markers if m and len(m) > 3)


def load_registry(dest_dir: Path) -> dict:
    path = dest_dir / _REGISTRY_NAME
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"hashes": {}, "archived_at": ""}


def save_registry(dest_dir: Path, registry: dict) -> None:
    registry["archived_at"] = datetime.now().isoformat(timespec="seconds")
    (dest_dir / _REGISTRY_NAME).write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def belongs_to_project(path: Path, markers: list[str]) -> bool:
    """读内容判断这份 plan 是不是本项目的。"""
    text = read_text(path).lower()
    return any(marker.lower() in text for marker in markers)


def content_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def target_name(source_dir: Path, path: Path, dest_dir: Path) -> str:
    """归档后的文件名。撞名了才加工具前缀，平时保持原名好认。"""
    stem = path.stem
    if stem.endswith(".plan"):
        stem = stem[: -len(".plan")]
    name = f"{stem}.plan.md"
    if (dest_dir / name).exists():
        tool = source_dir.parent.name.lstrip(".")
        name = f"{tool}-{stem}.plan.md"
    return name


def collect(repo_root: Path, dest_dir: Path) -> tuple[list[tuple[Path, Path, str]], dict]:
    """扫各工具目录，返回 [(源文件, 归档目标, hash)] 和统计。"""
    markers = project_markers(repo_root)
    registry = load_registry(dest_dir)
    seen_hashes = set(registry.get("hashes", {}).keys())
    pending: list[tuple[Path, Path, str]] = []
    stats = {"scanned": 0, "matched": 0, "skipped": 0, "source_dirs": 0}

    for tool, source_dir in _TOOL_PLAN_DIRS:
        if not source_dir.is_dir():
            continue
        stats["source_dirs"] += 1
        # rglob 递归：有的工具会把旧 plan 挪进子目录
        # （Cursor 的 `_archived/` 里就还有 252 份，只扫顶层会整批漏掉）
        for path in sorted(source_dir.rglob("*.md")):
            stats["scanned"] += 1
            if not belongs_to_project(path, markers):
                continue
            stats["matched"] += 1
            digest = content_digest(path)
            if not digest or digest in seen_hashes:
                stats["skipped"] += 1          # 已经收过了
                continue
            target = dest_dir / target_name(source_dir, path, dest_dir)
            pending.append((path, target, digest))
            seen_hashes.add(digest)

    stats["markers"] = len(markers)
    return pending, stats


def run(repo_root: Path, dry_run: bool = False) -> int:
    dest_dir = repo_root / "docs" / "plans"
    dest_dir.mkdir(parents=True, exist_ok=True)

    pending, stats = collect(repo_root, dest_dir)
    print(f"项目：{repo_root}")
    print(f"扫描 {stats['source_dirs']} 个工具目录、{stats['scanned']} 份 plan，"
          f"认领 {stats['matched']} 份（已归档 {stats['skipped']} 份）")

    if not pending:
        print("没有新的要归档。")
        return 0

    print(f"\n{'[dry-run] ' if dry_run else ''}待归档 {len(pending)} 份：")
    for source, target, _digest in pending:
        tool = source.parent.parent.name
        print(f"  {tool:<12} {source.name}  →  {target.name}")

    if dry_run:
        return 0

    registry = load_registry(dest_dir)
    archived = 0
    for source, target, digest in pending:
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            print(f"  复制失败：{source.name}（{exc}）")
            continue
        registry.setdefault("hashes", {})[digest] = target.name
        archived += 1
    save_registry(dest_dir, registry)
    print(f"\n已归档 {archived} 份到 {dest_dir}")
    print("记得在 docs/plans/README.md 的索引表里补一行。")
    return 0


def show_list() -> int:
    print("各 AI 工具的 Plan 目录：")
    for tool, source_dir in _TOOL_PLAN_DIRS:
        if not source_dir.is_dir():
            print(f"  {tool:<12} （不存在）")
            continue
        files = sorted(source_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        newest = datetime.fromtimestamp(files[0].stat().st_mtime).strftime("%Y-%m-%d") if files else "—"
        print(f"  {tool:<12} {len(files):>3} 份，最新 {newest}　{source_dir}")
    return 0


_HOOK_TEMPLATE = """#!/bin/sh
# 提交前自动把各 AI 工具的 plan 归档进 docs/plans/
# 由 scripts/archive_plans.py --install-hook 安装
root="$(git rev-parse --show-toplevel)"
# 优先用项目自己的 venv——系统 PATH 里未必有 python，装了钩子却不生效最气人
if [ -x "$root/.venv/Scripts/python.exe" ]; then
    py="$root/.venv/Scripts/python.exe"
elif [ -x "$root/.venv/bin/python" ]; then
    py="$root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    py="python3"
else
    py="python"
fi
"$py" "$root/scripts/archive_plans.py" || true
"""


def install_hook(repo_root: Path) -> int:
    hook = repo_root / ".git" / "hooks" / "pre-commit"
    if hook.exists() and "archive_plans" not in read_text(hook):
        backup = hook.with_suffix(".bak")
        shutil.copy2(hook, backup)
        print(f"已有 pre-commit，先备份到 {backup.name}")
    hook.write_text(_HOOK_TEMPLATE, encoding="utf-8", newline="\n")
    try:
        hook.chmod(0o755)
    except OSError:
        pass
    print(f"已装钩子：{hook}")
    print("之后每次 git commit 都会先自动归档一遍。")
    print("注意：如果 python 不在 PATH 里，请把钩子里的 python 换成绝对路径。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="归档各 AI 工具的 Plan 产物到 docs/plans/")
    parser.add_argument("--dry-run", action="store_true", help="只列出会归档什么，不动文件")
    parser.add_argument("--list", action="store_true", help="列出各工具 plans 目录现状")
    parser.add_argument("--install-hook", action="store_true", help="装 git pre-commit 钩子")
    args = parser.parse_args()

    if args.list:
        return show_list()

    repo_root = find_repo_root(Path(__file__).parent)
    if repo_root is None:
        print("找不到项目根（往上没看到 .git）", file=sys.stderr)
        return 1
    if args.install_hook:
        return install_hook(repo_root)
    return run(repo_root, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
