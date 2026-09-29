"""剪贴板工具：读写文本 / 截屏入剪贴板。"""

from __future__ import annotations

from screen_agent.tools.registry import PlatformError
from screen_agent.voice.executor import ActionResult


def clip_get_text() -> ActionResult:
    from screen_agent.tools import _win

    try:
        text = _win.clipboard_get_text()
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    preview = text[:80] + ("…" if len(text) > 80 else "")
    if not text:
        return ActionResult(success=True, message="剪贴板是空的", detail={"text": ""})
    return ActionResult(success=True, message=f"剪贴板内容：{preview}", detail={"text": text})


def clip_set_text(text: str) -> ActionResult:
    from screen_agent.tools import _win

    if not text:
        return ActionResult(success=False, message="要复制的内容是空的")
    try:
        _win.clipboard_set_text(text)
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    preview = text[:60] + ("…" if len(text) > 60 else "")
    return ActionResult(success=True, message=f"已复制到剪贴板：{preview}", detail={"text": text})
