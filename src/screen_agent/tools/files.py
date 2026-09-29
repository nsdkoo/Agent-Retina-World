"""文件工具：打开路径 / 列目录 / 模糊找文件。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from screen_agent.voice.executor import ActionResult

_SEARCH_ROOTS = ("Desktop", "Downloads", "Documents")
_SEARCH_DEPTH = 3
_SEARCH_LIMIT = 10


def open_path(path: str) -> ActionResult:
    """用系统默认方式打开文件/文件夹（os.startfile 委托 shell）。"""
    target = Path(path).expanduser()
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
    target = Path(path).expanduser()
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
