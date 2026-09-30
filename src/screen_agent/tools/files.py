"""文件工具：读（打开 / 列目录 / 找文件）+ 写（新建 / 移动 / 复制 / 重命名 / 回收站删除 / 归档 / 撤销）。

写入侧安全模型（对标 AI File Sorter 的 dry-run 预览、Desktop-AI-Organizer 的操作日志、
block/goose 的权限分级）：

- **白名单**：写操作只允许落在用户目录（桌面 / 下载 / 文档 / 图片 / 视频 / 音乐），
  系统目录、程序目录一律拒绝
- **回收站**：删除走 SHFileOperation + FOF_ALLOWUNDO，不提供永久删除
- **操作日志**：每次写操作写 journal，「撤销」可整体回滚上一步
- **两阶段归档**：先出「源 → 目标」清单，用户确认后才移动
- **重名不覆盖**：目标已存在时自动加 _1 / _2 后缀
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import time
from functools import lru_cache
from pathlib import Path

from screen_agent.tools.base import ActionResult

_SEARCH_ROOTS = ("Desktop", "Downloads", "Documents")
_SEARCH_DEPTH = 3
_SEARCH_LIMIT = 10

# 用户目录：中文别名 → 家目录下的真实名（OneDrive 重定向在 _user_dir 兜底）
_DIR_ALIASES = {
    "桌面": "Desktop", "desktop": "Desktop",
    "下载": "Downloads", "downloads": "Downloads",
    "文档": "Documents", "documents": "Documents",
    "图片": "Pictures", "pictures": "Pictures",
    "视频": "Videos", "videos": "Videos",
    "音乐": "Music", "music": "Music",
}

# 写操作可达的根目录白名单
_WRITE_ROOTS = ("Desktop", "Downloads", "Documents", "Pictures", "Videos", "Music")

# 归档时跳过的：系统文件 + 快捷方式（桌面上放快捷方式本来就是正常状态）
_SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
_SKIP_SUFFIX = {".lnk", ".url", ".ini", ".tmp"}

# 按类型归档的分类表（规则覆盖八成文件，剩下的落「其他」）
_CATEGORIES: dict[str, set[str]] = {
    "图片": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic",
             ".tif", ".tiff", ".ico", ".psd", ".raw", ".avif"},
    "文档": {".doc", ".docx", ".pdf", ".txt", ".md", ".xls", ".xlsx", ".ppt",
             ".pptx", ".csv", ".rtf", ".odt", ".wps", ".et", ".dps", ".epub"},
    "压缩包": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "安装包": {".exe", ".msi", ".apk", ".dmg", ".pkg", ".deb"},
    "音视频": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".mp4", ".mkv",
               ".avi", ".mov", ".wmv", ".flv", ".webm", ".srt"},
    "代码": {".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".java", ".go", ".rs",
             ".c", ".cpp", ".h", ".hpp", ".cs", ".json", ".yaml", ".yml", ".xml",
             ".html", ".css", ".scss", ".sh", ".ps1", ".bat", ".sql", ".ipynb"},
}


# ---------------------------------------------------------------- 路径与安全

@lru_cache(maxsize=32)
def _user_dir(name: str) -> Path:
    """家目录下的标准目录；OneDrive 重定向的机器走第二个候选。"""
    home = Path.home()
    for candidate in (home / name, home / "OneDrive" / name):
        if candidate.is_dir():
            return candidate
    return home / name


def resolve_dir(text: str = "") -> Path:
    """把「桌面」「下载」这类说法换成真实目录；给了路径就原样用。空值默认桌面。"""
    raw = (text or "").strip().strip("「」\"'")
    if not raw:
        return _user_dir("Desktop")
    path = Path(raw).expanduser()
    if path.is_dir():
        return path
    lowered = raw.lower()
    if lowered in _DIR_ALIASES:
        return _user_dir(_DIR_ALIASES[lowered])
    # 「桌面的文件」「下载里那些」这类带尾巴的说法，命中别名即可
    for alias, name in _DIR_ALIASES.items():
        if alias in lowered:
            return _user_dir(name)
    return path


def _guard_write(target: Path) -> str | None:
    """写操作白名单校验：返回拒绝原因，None 表示放行。"""
    try:
        resolved = target.expanduser().resolve()
    except OSError:
        return f"路径没法解析：{target}"
    for name in _WRITE_ROOTS:
        root = _user_dir(name)
        try:
            resolved.relative_to(root.resolve())
            return None
        except (ValueError, OSError):
            continue
    return f"为了安全，我只在桌面 / 下载 / 文档 / 图片 / 视频 / 音乐里动手，不碰 {target}"


def _unique_target(dst: Path) -> Path:
    """目标已存在时加 _1 / _2 后缀，绝不覆盖。"""
    if not dst.exists():
        return dst
    for index in range(1, 1000):
        candidate = dst.parent / f"{dst.stem}_{index}{dst.suffix}"
        if not candidate.exists():
            return candidate
    return dst.parent / f"{dst.stem}_{int(time.time())}{dst.suffix}"


def _resolve_input(text: str) -> Path:
    """把用户说的文件位置解析成 Path（支持「下载」「桌面/报告.pdf」这类别名）。"""
    raw = (text or "").strip().strip("「」\"'")
    if not raw:
        return Path(raw)
    if raw.lower() in _DIR_ALIASES:
        return _user_dir(_DIR_ALIASES[raw.lower()])
    normalized = raw.replace("\\", "/")
    head, _, tail = normalized.partition("/")
    if tail and head.lower() in _DIR_ALIASES:
        return _user_dir(_DIR_ALIASES[head.lower()]) / tail
    return Path(raw).expanduser()


def _resolve_dst(text: str, src: Path) -> Path:
    """目标解析：是目录就搬进去，否则当作完整目标路径。"""
    raw = (text or "").strip().strip("「」\"'")
    if not raw:
        return src.parent / src.name
    dst = _resolve_input(raw)
    if dst.is_dir():
        return dst / src.name
    return dst


# ---------------------------------------------------------------- 操作日志

def _journal_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "cache" / "fs_journal.json"


def _pending_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "cache" / "pending_organize.json"


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _record(entries: list[dict]) -> None:
    if entries:
        _write_json(_journal_path(), {"updated": time.time(), "entries": entries})


def undo_last() -> ActionResult:
    """撤销上一次写操作（归档 / 移动 / 复制 / 新建文件夹）。删除的走回收站还原。"""
    journal = _read_json(_journal_path())
    entries = journal.get("entries") or []
    if not entries:
        return ActionResult(success=False, message="没有可撤销的操作")
    restored, failed, deleted = 0, [], []
    for entry in reversed(entries):
        try:
            op = entry.get("op")
            if op in ("move", "rename"):
                src, dst = Path(entry["src"]), Path(entry["dst"])
                if not dst.exists():
                    failed.append(src.name)
                    continue
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                restored += 1
            elif op == "copy":
                dst = Path(entry["dst"])
                if dst.exists():
                    dst.unlink()
                    restored += 1
            elif op == "mkdir":
                folder = Path(entry["dst"])
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
                    restored += 1
            elif op == "delete":
                deleted.append(Path(entry["src"]).name)
        except OSError as exc:
            failed.append(f"{entry.get('src')}: {exc}")
    _journal_path().unlink(missing_ok=True)
    parts = [f"已撤销 {restored} 项"] if restored else ["没有可回滚的项目"]
    if deleted:
        parts.append(f"{len(deleted)} 个删掉的文件请到回收站还原：{'、'.join(deleted[:5])}")
    if failed:
        parts.append(f"{len(failed)} 项没能还原")
    return ActionResult(success=restored > 0 or bool(deleted), message="，".join(parts))


# ---------------------------------------------------------------- 读（原有）

def open_path(path: str) -> ActionResult:
    """用系统默认方式打开文件/文件夹（os.startfile 委托 shell）。"""
    target = _resolve_input(path)
    if not target.exists():
        return ActionResult(success=False, message=f"路径不存在：{target}")
    if not hasattr(os, "startfile"):
        return ActionResult(success=False, message="此功能仅支持 Windows")
    try:
        os.startfile(str(target))  # type: ignore[attr-defined]
    except OSError as exc:
        return ActionResult(success=False, message=f"打开失败：{exc}")
    return ActionResult(success=True, message=f"已打开：{target.name}", detail={"path": str(target)})


def list_dir(path: str = "~") -> ActionResult:
    target = _resolve_input(path) if path not in ("", "~") else Path.home()
    if not target.is_dir():
        return ActionResult(success=False, message=f"不是目录：{target}")
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = [f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries[:30]]
    more = f"\n（共 {len(entries)} 项，仅显示前 30）" if len(entries) > 30 else ""
    return ActionResult(
        success=True,
        message=f"{target} 下 {len(entries)} 项：\n" + "\n".join(lines) + more,
        detail={"count": len(entries)},
    )


def find_files(pattern: str, base: str | None = None) -> ActionResult:
    """常见用户目录模糊找文件（Desktop/Downloads/Documents，深度 3，大小写不敏感）。"""
    if not pattern:
        return ActionResult(success=False, message="告诉我文件名关键词")
    needle = pattern.lower()
    home = Path.home()
    roots = [Path(base).expanduser()] if base else [home / r for r in _SEARCH_ROOTS]
    hits: list[str] = []
    deadline = time.monotonic() + 4.0  # 硬超时防全盘扫
    for root in roots:
        if not root.exists():
            continue
        current_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if len(Path(dirpath).parts) - current_depth >= _SEARCH_DEPTH:
                dirnames[:] = []
            if time.monotonic() > deadline:
                break
            for name in filenames:
                if needle in name.lower():
                    hits.append(str(Path(dirpath) / name))
                    if len(hits) >= _SEARCH_LIMIT:
                        break
            if len(hits) >= _SEARCH_LIMIT:
                break
        if hits or time.monotonic() > deadline:
            break
    if not hits:
        return ActionResult(success=False, message=f"常用目录里没找到「{pattern}」")
    listing = "\n".join(f"- {h}" for h in hits)
    return ActionResult(success=True, message=f"找到 {len(hits)} 个：\n{listing}", detail={"hits": hits})


# ---------------------------------------------------------------- 写：单个操作

def make_dir(path: str) -> ActionResult:
    """新建文件夹。只给名字时落在桌面。"""
    raw = (path or "").strip().strip("「」\"'")
    if not raw:
        return ActionResult(success=False, message="告诉我文件夹叫什么")
    target = _resolve_input(raw)
    if "/" not in raw and "\\" not in raw:
        target = _user_dir("Desktop") / raw
    reason = _guard_write(target)
    if reason:
        return ActionResult(success=False, message=reason)
    if target.exists():
        return ActionResult(success=False, message=f"已经存在：{target}")
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        return ActionResult(success=False, message=f"新建失败：{exc}")
    _record([{"op": "mkdir", "dst": str(target)}])
    return ActionResult(success=True, message=f"已新建文件夹：{target.name}", detail={"path": str(target)})


def move_path(src: str, dst: str) -> ActionResult:
    source = _resolve_input(src)
    if not source.exists():
        return ActionResult(success=False, message=f"找不到：{source}")
    target = _resolve_dst(dst, source)
    reason = _guard_write(target) or _guard_write(source)
    if reason:
        return ActionResult(success=False, message=reason)
    if target.exists():
        target = _unique_target(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        return ActionResult(success=False, message=f"移动失败：{exc}")
    _record([{"op": "move", "src": str(source), "dst": str(target)}])
    return ActionResult(
        success=True,
        message=f"已移动：{source.name} → {target.parent.name}",
        detail={"src": str(source), "dst": str(target)},
    )


def copy_path(src: str, dst: str) -> ActionResult:
    source = _resolve_input(src)
    if not source.exists():
        return ActionResult(success=False, message=f"找不到：{source}")
    target = _resolve_dst(dst, source)
    reason = _guard_write(target)
    if reason:
        return ActionResult(success=False, message=reason)
    if target.exists():
        target = _unique_target(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(str(source), str(target))
        else:
            shutil.copy2(str(source), str(target))
    except OSError as exc:
        return ActionResult(success=False, message=f"复制失败：{exc}")
    _record([{"op": "copy", "src": str(source), "dst": str(target)}])
    return ActionResult(
        success=True,
        message=f"已复制：{source.name} → {target.parent.name}",
        detail={"src": str(source), "dst": str(target)},
    )


def rename_path(src: str, new_name: str) -> ActionResult:
    source = _resolve_input(src)
    if not source.exists():
        return ActionResult(success=False, message=f"找不到：{source}")
    clean = (new_name or "").strip().strip("「」\"'")
    if not clean:
        return ActionResult(success=False, message="告诉我改成什么名字")
    if not Path(clean).suffix and source.is_file():
        clean += source.suffix  # 只给了主名，保留原后缀
    target = source.parent / clean
    reason = _guard_write(target)
    if reason:
        return ActionResult(success=False, message=reason)
    if target.exists() and target != source:
        return ActionResult(success=False, message=f"「{clean}」已经存在了")
    try:
        source.rename(target)
    except OSError as exc:
        return ActionResult(success=False, message=f"重命名失败：{exc}")
    _record([{"op": "rename", "src": str(source), "dst": str(target)}])
    return ActionResult(success=True, message=f"已重命名：{target.name}", detail={"path": str(target)})


# ---- 回收站删除（SHFileOperation + FOF_ALLOWUNDO，不永久删）----

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    from ctypes import wintypes as _w  # noqa: PLC0415 - 结构体字段需在类体内解析

    _fields_ = [
        ("hwnd", _w.HWND),
        ("wFunc", _w.UINT),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", _w.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def _send_to_recycle_bin(target: Path) -> None:
    if sys.platform != "win32":
        raise OSError("回收站删除仅支持 Windows")
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.normpath(str(target)) + "\0\0"  # 双 NUL 结尾
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    code = shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0:
        raise OSError(f"SHFileOperation 返回 {code}")
    if op.fAnyOperationsAborted:
        raise OSError("操作被中止")


def delete_path(path: str) -> ActionResult:
    """放进回收站（可还原）。目录只接受空目录，不做递归删除。"""
    target = _resolve_input(path)
    reason = _guard_write(target)
    if reason:
        return ActionResult(success=False, message=reason)
    if not target.exists():
        return ActionResult(success=False, message=f"找不到：{target}")
    if target.is_dir():
        try:
            if any(target.iterdir()):
                return ActionResult(success=False, message=f"「{target.name}」里还有东西，我只删空文件夹")
        except OSError as exc:
            return ActionResult(success=False, message=f"读不了这个目录：{exc}")
    try:
        _send_to_recycle_bin(target)
    except OSError as exc:
        return ActionResult(success=False, message=f"删除失败：{exc}")
    _record([{"op": "delete", "src": str(target)}])
    return ActionResult(
        success=True,
        message=f"已放进回收站：{target.name}（要还原就去回收站找）",
        detail={"path": str(target)},
    )


# ---------------------------------------------------------------- 写：桌面归档

def _category_of(suffix: str) -> str:
    lowered = suffix.lower()
    for name, kinds in _CATEGORIES.items():
        if lowered in kinds:
            return name
    return "其他"


def _date_bucket(target: Path) -> str:
    import datetime as dt

    mtime = dt.datetime.fromtimestamp(target.stat().st_mtime)
    now = dt.datetime.now()
    if (mtime.year, mtime.month) == (now.year, now.month):
        return "本月"
    previous = now.replace(day=1) - dt.timedelta(days=1)
    if (mtime.year, mtime.month) == (previous.year, previous.month):
        return "上月"
    if mtime.year == now.year:
        return "今年更早"
    return f"{mtime.year} 年"


def _build_plan(target: Path, mode: str) -> list[tuple[Path, Path]]:
    """出「源 → 目标」清单。跳过子目录、系统文件和快捷方式。"""
    plan: list[tuple[Path, Path]] = []
    try:
        entries = sorted(target.iterdir())
    except OSError:
        return plan
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        name = entry.name.lower()
        if name in _SKIP_NAMES or entry.suffix.lower() in _SKIP_SUFFIX:
            continue
        bucket = _date_bucket(entry) if mode == "date" else _category_of(entry.suffix)
        plan.append((entry, target / bucket / entry.name))
    return plan


def _save_pending(target: Path, mode: str, stage: str, plan: list[tuple[Path, Path]]) -> None:
    _write_json(_pending_path(), {
        "updated": time.time(),
        "target": str(target),
        "mode": mode,
        "stage": stage,
        "plan": [[str(s), str(d)] for s, d in plan],
    })


def _load_pending() -> dict:
    return _read_json(_pending_path())


def _clear_pending() -> None:
    _pending_path().unlink(missing_ok=True)


def _run_plan(plan: list[tuple[Path, Path]], target: Path) -> ActionResult:
    moved, failed, entries = 0, [], []
    for src, dst in plan:
        reason = _guard_write(dst)
        if reason:
            failed.append(f"{src.name}：{reason}")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            final = _unique_target(dst)
            shutil.move(str(src), str(final))
            entries.append({"op": "move", "src": str(src), "dst": str(final)})
            moved += 1
        except OSError as exc:
            failed.append(f"{src.name}：{exc}")
    _record(entries)
    if not moved:
        return ActionResult(
            success=False,
            message=f"一个都没动成：{failed[0] if failed else '没有可移动的文件'}",
            detail={"moved": 0, "failed": failed[:5]},
        )
    buckets = sorted({dst.parent.name for _, dst in plan})
    text = f"整理完成：{moved} 个文件归到 {target.name} 下的 {len(buckets)} 个文件夹（{'、'.join(buckets)}）"
    if failed:
        text += f"，{len(failed)} 个跳过"
    text += "。说「撤销」可以还原"
    return ActionResult(success=True, message=text, detail={"moved": moved, "failed": failed[:5]})


def organize_dir(path: str = "", mode: str = "", apply: bool = False) -> ActionResult:
    """目录归档三阶段：问怎么整理 → 出清单待确认 → 执行。

    - mode=""    → 先问用户想按什么规则整理（返回 options）
    - mode=type  → 按文件类型（图片/文档/压缩包…）
    - mode=date  → 按修改时间（本月/上月/今年更早/更早年份）
    - apply=True → 执行上一次确认过的清单
    """
    pending = _load_pending()

    if mode == "cancel":
        _clear_pending()
        return ActionResult(success=True, message="好，那先不动")

    if apply:
        if not pending or pending.get("stage") != "confirm":
            return ActionResult(success=False, message="我这边没有待确认的归档计划，先说一声「整理桌面」")
        target = Path(pending.get("target", ""))
        plan = [(Path(s), Path(d)) for s, d in pending.get("plan", [])]
        _clear_pending()
        if not target.is_dir():
            return ActionResult(success=False, message=f"目录不见了：{target}")
        return _run_plan(plan, target)

    target = resolve_dir(path or pending.get("target", ""))
    if not target.is_dir():
        return ActionResult(success=False, message=f"没找到目录：{target}")

    chosen = mode or pending.get("mode") or ""
    plan = _build_plan(target, chosen or "type")
    if not plan:
        _clear_pending()
        return ActionResult(success=True, message=f"{target.name}挺整齐，没有需要归类的文件")

    if chosen in ("type", "date"):
        _save_pending(target, chosen, "confirm", plan)
        label = "按类型" if chosen == "type" else "按修改时间"
        preview = "\n".join(f"  {s.name} → {d.parent.name}/" for s, d in plan[:12])
        more = f"\n  …还有 {len(plan) - 12} 个" if len(plan) > 12 else ""
        buckets = sorted({d.parent.name for _, d in plan})
        return ActionResult(
            success=True,
            message=(
                f"{label}归档 {len(plan)} 个文件到 {len(buckets)} 个文件夹"
                f"（{'、'.join(buckets)}）：\n{preview}{more}"
            ),
            detail={"plan": [[str(s), str(d)] for s, d in plan]},
            options=["确认归档", "算了，先不动"],
        )

    _save_pending(target, "", "ask_mode", [])
    return ActionResult(
        success=True,
        message=f"{target.name}上有 {len(plan)} 个文件，你想怎么整理？",
        detail={"count": len(plan)},
        options=["按类型归档", "按修改时间归档", "先不动"],
    )


def organize_cancel() -> ActionResult:
    _clear_pending()
    return ActionResult(success=True, message="好，那先不动")
