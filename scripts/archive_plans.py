#!/usr/bin/env python
"""Plan 自动归档（项目版）—— 跟着仓库走的那一份。

## 它解决什么

AI 工具的 Plan 模式会把计划落成 md 文件，散落在各自的目录里
（`~/.workbuddy/plans/`、`~/.cursor/plans/`、`~/.codex/plans/`…），
**不会自动进项目仓库**。以前靠人工搬运，靠记性必然断。

这个脚本挂到各 AI 工具的 hook 上（会话结束 / 开始），
每次自动把属于当前项目的 plan 收进 `<项目>/docs/plans/`。

## 和项目内 scripts/archive_plans.py 的区别

- **项目内的这份**：跟着仓库走，便于团队协作、无 hook 时手动跑
- **全局那份**（`~/.plan-archive/`）：被各 AI 工具的 hook 调用，**服务所有项目**

**两者逻辑相同**，全局那份是最新的。改逻辑时两边都要改。

两者逻辑相同。项目根从**工作目录**推断（hook 触发时 cwd 就是项目根）。

## 用法

    python archive_plans.py                  # 用当前目录推断项目
    python archive_plans.py --cwd <项目路径>  # 指定项目
    python archive_plans.py --dry-run        # 只看会归档什么
    python archive_plans.py --list           # 看各工具 plans 目录现状

## 设计要点

- **静默友好**：hook 调用时用户看不到输出，所以找不到项目就直接安静退出，
  过程写进 `~/.plan-archive/archive.log`
- **幂等**：按内容 hash 登记，重复跑不会产生重复文件
- **快**：hook 的 timeout 可能只有几秒，所以扫描要克制
- **零配置**：项目识别信号自动从项目结构提取（见 `project_markers`）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
LOG_PATH = HOME / ".plan-archive" / "archive.log"

# 明显不是 AI 工具、或不该扫的目录（系统配置 / 缓存 / 虚拟环境）
_SKIP_DIRS = {
    ".git", ".ssh", ".gnupg", ".cache", ".config", ".docker", ".npm", ".gradle", ".m2",
    ".vscode", ".local", ".azure", ".dotnet", ".templateengine", ".cache-usage",
    ".plan-archive",          # 自己的目录，别把运行日志当 plan
    ".thumbnails", ".idlerc", ".matplotlib", ".ipython", ".pytest_cache",
    # 这几个是踩坑加上的：项目内不限制隐藏目录时，.venv 有几千个条目，
    # 一次 iterdir 就要几十秒，hook 直接被卡死
    ".venv", "venv", ".env", "node_modules", "site-packages", "__pycache__",
    ".mypy_cache", ".ruff_cache", ".tox", ".eggs", "dist-info",
}

_REGISTRY_NAME = ".archive-registry.json"
# 项目可以在根目录放这个文件补充识别信号（都是可选的）
_CONFIG_NAME = ".plan-archive.json"

# 哪些后缀算 plan 文件。**不写死 .md** —— 不同工具可能用别的格式
_PLAN_SUFFIXES = (".md", ".markdown", ".txt")

# 目录名里出现这些词就认作 plan 目录（大小写不敏感）
_PLAN_DIR_HINTS = ("plan",)

# 单次扫描的文件上限——防止某个目录爆量时把 hook 卡死
_MAX_SCAN = 2000


def log(message: str) -> None:
    """写日志。hook 场景下用户看不到 stdout，所以要落盘。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _files_in_plan_dir(directory: Path, depth: int = 1):
    """列出 plan 目录里的文件。**限制递归深度**。

    不做无限递归的原因：`rglob("*")` 一旦落到 `.cursor/` 这种大目录上，
    会把 extensions 里上万个文件全走一遍，直接把 hook 卡死（踩过）。

    depth=1 足够覆盖常见的一层归档结构（比如 Cursor 的 `_archived/`）。
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.suffix.lower() in _PLAN_SUFFIXES:
                yield entry
            elif entry.is_dir() and depth > 0 and entry.name.lower() not in _SCAN_EXCLUDE:
                yield from _files_in_plan_dir(entry, depth - 1)
        except OSError:
            continue


# 这些子目录是数据/缓存，不是计划产物——扫它们纯属浪费
_SCAN_EXCLUDE = {
    "logs", "log", "cache", "extensions", "blobs", "sessions", "chats", "history",
    "node_modules", "vendor", "tmp", "debug", "traces", "trace", "storage",
    "sandbox", "backup", "backups", "snapshots", "clipboard-images", "audit-log",
    "connectors", "plugins", "binaries", "artifact-index", "models", "images",
}

# 判断「像不像一份计划」用的词。**这是第 ③ 层兜底的依据**——
# 工具随时可能改目录名、改格式，所以最终得靠内容自己说话。
# 刻意**只收中文的强信号词**：英文 "plan" 出现频率太高（planning / planned…），
# 放进来会把规则文档、说明文档全卷进来
_PLAN_TITLE_HINTS = ("计划", "规划", "实施方案", "执行方案", "改造方案", "roadmap")

# 计划文档通常有的结构标记（有标题 + 有结构，才敢认）
_PLAN_STRUCTURE_HINTS = ("## ", "- [ ]", "- [x]", "步骤", "阶段", "目标", "todo", "里程碑")

# 这些文件名天生不是计划 —— 它们是规则/说明/元数据文件。
# 踩过的坑：`.dsh/AGENTS.md`（全局 AI 规则）因为有 `##` 标题被判成计划收走了
_NOT_PLAN_NAMES = {
    "agents.md", "claude.md", "gemini.md", "readme.md", "contributing.md",
    "changelog.md", "license.md", "cursorrules", "copilot-instructions.md",
    "settings.md", "memory.md", "user.md", "soul.md", "identity.md",
    "package.md", "install.md", "usage.md", "faq.md", "index.md",
}


# 单次扫描的时间预算（秒）。hook 的 timeout 只有几秒，**绝不能因为扫描把会话卡住**。
# 超预算立即返回已发现的结果——宁可少收，不可拖慢用户的正常流程
_SCAN_BUDGET = 3.0

# 每个工具目录最多做几次内容判断（第 ③ 层最贵，必须限量）
_CONTENT_PROBE_LIMIT = 20


def _safe_resolve(path: Path) -> Path | None:
    """解析真实路径，失败返回 None（软链坏了、权限不足等都不该炸）。"""
    try:
        return path.resolve()
    except OSError:
        return None


def looks_like_plan(path: Path) -> bool:
    """按**内容**判断一个文本文件像不像计划文档。

    这是第 ③ 层兜底：工具会更新，目录名会改，格式会换 ——
    预设清单永远慢一步，所以最后得有一个「自己看内容判断」的机制。

    判据故意保守（**宁可漏、不可错**）：
    文件名或开头 300 字里出现计划类词，**并且**正文有结构标记。
    单独命中任一条都不算 —— 否则一堆 README 都会被误收。

    已知的规则/说明类文件名直接排除（见 `_NOT_PLAN_NAMES`）。
    """
    name = path.name.lower()
    if name in _NOT_PLAN_NAMES:
        return False
    if "plan" in name or "计划" in path.name or "roadmap" in name:
        return True

    try:
        if path.stat().st_size > 200_000:      # 太大的不看，计划不该那么大
            return False
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    head = text[:300]
    if not any(hint in head for hint in _PLAN_TITLE_HINTS):
        return False
    return any(mark in text for mark in _PLAN_STRUCTURE_HINTS)


def discover_plan_sources(project_root: Path | None = None) -> list[tuple[str, Path, int]]:
    """动态发现计划产物 —— **三层递进，越往后越宽松**。

    为什么必须动态：本机 `~/.` 下有 20 多个 AI 工具目录且还在增加；
    工具还会自我更新（改目录名、改落盘格式）。写死清单等于每次更新都漏一批。

    ① **目录名含 plan** —— 常规情况，最可靠
    ② **文件名含 plan** —— 工具把目录改了名也能找到
    ③ **内容特征判断** —— 藏得深、名字完全不像的（兜底，限量做）

    **性能是硬约束**：初版用「遍历所有文件」实现，40+ 个工具目录下
    扫了 2 分钟都没完，会把 hook 直接卡死。改成让**文件系统自己过滤**
    （glob 比 Python 遍历快几个数量级），再加**时间预算**兜底。
    """
    found: list[tuple[str, Path, int]] = []
    seen: set[Path] = set()
    deadline = time.monotonic() + _SCAN_BUDGET

    roots: list[tuple[Path, bool]] = [(HOME, True)]
    if project_root is not None:
        # **项目内也只扫隐藏目录**：AI 工具的配置目录都是 `.xxx` 形式。
        # 扫非隐藏目录会把 .venv / data / models / assets 全 iterdir 一遍，
        # 那是几十秒的开销（踩过这个坑）。
        roots.append((project_root, True))

    for root, only_hidden in roots:
        if not root.is_dir():
            continue
        try:
            tool_dirs = [c for c in root.iterdir() if c.is_dir()]
        except OSError:
            continue

        for tool_dir in tool_dirs:
            if time.monotonic() > deadline:
                log(f"发现阶段超预算，已提前返回 {len(found)} 个目录")
                return found
            if tool_dir.name in _SKIP_DIRS:
                continue
            if only_hidden and not tool_dir.name.startswith("."):
                continue
            tool = tool_dir.name.lstrip(".")

            # **每个工具目录只 iterdir 一次**，后续全用这份结果。
            # 踩过的坑：初版对每个工具目录做 12 次 glob（depth × pattern × suffix），
            # 40 个工具 = 480 次目录遍历，而 .cursor 那种目录有上万个条目，
            # 30 秒都跑不完 —— hook 会被直接卡死。
            try:
                entries = list(tool_dir.iterdir())
            except OSError:
                continue
            subdirs = [e for e in entries if e.is_dir()]
            files_here = [e for e in entries if e.is_file()]
            hit_any = False

            # ① 子目录名含 plan（常规情况）
            for sub in subdirs:
                name = sub.name.lower()
                if name in _SCAN_EXCLUDE:
                    continue
                if any(hint in name for hint in _PLAN_DIR_HINTS):
                    real = _safe_resolve(sub)
                    if real and real not in seen:
                        seen.add(real)
                        found.append((tool, sub, 1))
                        hit_any = True

            # ② 本层文件名含 plan（工具改了目录名也能找到）
            for cand in files_here:
                if cand.suffix.lower() not in _PLAN_SUFFIXES:
                    continue
                low = cand.name.lower()
                if "plan" in low or "计划" in cand.name or "roadmap" in low:
                    real = _safe_resolve(cand.parent)
                    if real and real not in seen:
                        seen.add(real)
                        found.append((tool, cand.parent, 0))
                        hit_any = True

            # ③ 内容特征兜底：只在前两层一无所获时才做，且限量——
            #    这层要读文件，最贵
            if not hit_any:
                probed = 0
                for cand in files_here:
                    if probed >= _CONTENT_PROBE_LIMIT or time.monotonic() > deadline:
                        break
                    if cand.suffix.lower() not in _PLAN_SUFFIXES:
                        continue
                    probed += 1
                    if looks_like_plan(cand):
                        real = _safe_resolve(cand.parent)
                        if real and real not in seen:
                            seen.add(real)
                            found.append((tool, cand.parent, 0))
                            break
    return found


def find_repo_root(start: Path) -> Path | None:
    """从 start 往上找 `.git`。找不到说明不在项目里，调用方应当安静退出。"""
    try:
        current = start.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def auto_code_markers(repo_root: Path) -> list[str]:
    """自动提取项目特有的**代码路径**作为识别信号。

    AI 写计划时**更愿意写相对代码路径**（`src/screen_agent/tools/`），
    而不是项目绝对路径——实测有三份 plan 因为正文没写绝对路径而被漏掉，
    但通篇都在提 `src/screen_agent/`。包名是项目特有的，这条路最可靠。
    """
    markers: set[str] = set()
    for parent in ("src", "lib", "app", "packages"):
        base = repo_root / parent
        if not base.is_dir():
            continue
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not child.name.startswith((".", "_")):
                markers.add(f"{parent}/{child.name}")
    return sorted(markers)


def load_project_config(repo_root: Path) -> dict:
    """读项目自己的补充配置（可选）。

    **为什么需要它**：改过名的项目，旧 plan 里写的是旧名，光靠当前目录名认不出来。
    但历史名**不能做成全局常量** —— 那样每个项目都会去认领「提到过那个名字」的 plan，
    造成跨项目误收（踩过：Agent-Rita 的两份 plan 被 hbh-mall 项目收走了）。
    所以历史名只能由项目自己声明。
    """
    path = repo_root / _CONFIG_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log(f"{repo_root.name}: {_CONFIG_NAME} 解析失败，按无配置处理")
        return {}
    return data if isinstance(data, dict) else {}


def project_markers(repo_root: Path) -> list[str]:
    """本项目的识别信号。

    按可靠度：**代码路径** > 项目路径 > 项目名 > 项目自己声明的关键词。

    刻意**不含任何全局常量** —— 每个项目的信号只能来自它自己。
    """
    name = repo_root.name
    markers = {
        name,
        name.replace("-", "_"),
        str(repo_root),
        str(repo_root).replace("\\", "/"),
        str(repo_root).replace("/", "\\"),
    }
    markers.update(auto_code_markers(repo_root))

    config = load_project_config(repo_root)
    markers.update(config.get("markers", []))          # 项目自报的识别词
    markers.update(config.get("historical_names", []))  # 改过名的项目自己声明旧名

    return sorted(m for m in markers if m and len(m) > 3)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


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
    try:
        (dest_dir / _REGISTRY_NAME).write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def belongs_to_project(path: Path, markers: list[str]) -> bool:
    text = read_text(path).lower()
    return any(marker.lower() in text for marker in markers)


def target_name(tool: str, path: Path, dest_dir: Path) -> str:
    """归档后的文件名。撞名了才加工具前缀，平时保持原名好认。"""
    stem = path.stem
    if stem.endswith(".plan"):
        stem = stem[: -len(".plan")]
    name = f"{stem}.plan.md"
    if (dest_dir / name).exists():
        safe_tool = tool.replace("@", "-").replace(".", "")
        name = f"{safe_tool}-{stem}.plan.md"
    return name


def collect(repo_root: Path, dest_dir: Path):
    """扫所有发现的 plan 目录，认出属于本项目的。"""
    markers = project_markers(repo_root)
    registry = load_registry(dest_dir)
    seen = set(registry.get("hashes", {}).keys())
    pending: list[tuple[Path, Path, str]] = []
    stats = {"scanned": 0, "matched": 0, "skipped": 0, "dirs": 0}

    for tool, source_dir, depth in discover_plan_sources(repo_root):
        stats["dirs"] += 1
        for path in _files_in_plan_dir(source_dir, depth):
            if stats["scanned"] >= _MAX_SCAN:
                log(f"{repo_root.name}: 达到扫描上限 {_MAX_SCAN}，本轮提前结束")
                break
            stats["scanned"] += 1
            if not belongs_to_project(path, markers):
                continue
            stats["matched"] += 1
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            except OSError:
                continue
            if not digest or digest in seen:
                stats["skipped"] += 1
                continue
            pending.append((path, dest_dir / target_name(tool, path, dest_dir), digest))
            seen.add(digest)
    return pending, stats


def run(repo_root: Path, dry_run: bool = False, quiet: bool = False) -> int:
    def say(message: str) -> None:
        if not quiet:
            print(message)

    dest_dir = repo_root / "docs" / "plans"
    pending, stats = collect(repo_root, dest_dir)

    if not pending:
        log(f"{repo_root.name}: 扫描 {stats['scanned']} 份，认领 {stats['matched']} 份，无新增")
        say(f"扫描 {stats['scanned']} 份 plan，认领 {stats['matched']} 份，没有新的要归档。")
        return 0

    say(f"{'[dry-run] ' if dry_run else ''}待归档 {len(pending)} 份 → {dest_dir}")
    for source, target, _digest in pending:
        say(f"  {source.parent.parent.name:<12} {source.name}  →  {target.name}")

    if dry_run:
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    registry = load_registry(dest_dir)
    archived = 0
    for source, target, digest in pending:
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            log(f"{repo_root.name}: 复制失败 {source.name}（{exc}）")
            continue
        registry.setdefault("hashes", {})[digest] = target.name
        archived += 1
    save_registry(dest_dir, registry)
    log(f"{repo_root.name}: 归档 {archived} 份 → {dest_dir}")
    say(f"已归档 {archived} 份。")
    return 0


_HOOK_TEMPLATE = """#!/bin/sh
# 提交前自动把各 AI 工具的 plan 归档进 docs/plans/
# 由 scripts/archive_plans.py --install-hook 安装
root="$(git rev-parse --show-toplevel)"
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
    """装 git pre-commit 钩子：提交即留档，不靠记性。"""
    hook = repo_root / ".git" / "hooks" / "pre-commit"
    if hook.exists() and "archive_plans" not in read_text(hook):
        shutil.copy2(hook, hook.with_suffix(".bak"))
        print("已有 pre-commit，先备份")
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(_HOOK_TEMPLATE, encoding="utf-8", newline="\n")
    try:
        hook.chmod(0o755)
    except OSError:
        pass
    print(f"已装钩子：{hook}")
    return 0


def show_list(project_root: Path | None = None) -> int:
    """列出动态发现的所有 plan 目录。"""
    dirs = discover_plan_sources(project_root)
    print(f"动态发现 {len(dirs)} 个 plan 目录：")
    total = 0
    for tool, source_dir, depth in dirs:
        files = list(_files_in_plan_dir(source_dir, depth))
        total += len(files)
        newest = "—"
        if files:
            try:
                newest = datetime.fromtimestamp(
                    max(f.stat().st_mtime for f in files)
                ).strftime("%Y-%m-%d")
            except OSError:
                pass
        print(f"  {tool:<20} {len(files):>4} 份，最新 {newest}　{source_dir}")
    print(f"  {'合计':<20} {total:>4} 份")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把各 AI 工具的 Plan 产物归档进项目 docs/plans/")
    parser.add_argument("--cwd", default=None, help="项目路径（默认用当前工作目录）")
    parser.add_argument("--dry-run", action="store_true", help="只列出会归档什么")
    parser.add_argument("--list", action="store_true", help="列出各工具 plans 目录现状")
    parser.add_argument("--quiet", action="store_true", help="安静模式（hook 用）")
    parser.add_argument("--install-hook", action="store_true", help="装 git pre-commit 钩子")
    args = parser.parse_args()

    if args.list:
        return show_list()

    start = Path(args.cwd) if args.cwd else Path.cwd()
    repo_root = find_repo_root(start)
    if repo_root is None:
        # hook 会在非项目目录里触发（比如纯聊天会话），安静退出即可
        log(f"跳过：{start} 不在任何 git 项目里")
        return 0
    if args.install_hook:
        return install_hook(repo_root)
    return run(repo_root, dry_run=args.dry_run, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
