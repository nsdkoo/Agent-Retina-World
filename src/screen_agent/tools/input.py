"""输入原语：鼠标移动/点击、键盘输入、滚轮（SendInput）。"""

from __future__ import annotations

from screen_agent.tools.registry import PlatformError
from screen_agent.voice.executor import ActionResult


def mouse_move(x: int, y: int) -> ActionResult:
    from screen_agent.tools import _win

    try:
        _win.mouse_move_abs(x, y)
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    return ActionResult(success=True, message=f"鼠标已移到 ({x}, {y})", detail={"x": x, "y": y})


def mouse_click(x: int | None = None, y: int | None = None, button: str = "left", double: bool = False) -> ActionResult:
    from screen_agent.tools import _win

    try:
        if x is not None and y is not None:
            _win.mouse_move_abs(x, y)
        _win.mouse_click(button, double)
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    verb = "双击" if double else "点击"
    pos = f" ({x}, {y})" if x is not None and y is not None else ""
    return ActionResult(success=True, message=f"已{verb}{pos}", detail={"x": x, "y": y, "button": button})


def type_text(text: str) -> ActionResult:
    from screen_agent.tools import _win

    if not text:
        return ActionResult(success=False, message="要输入的内容是空的")
    try:
        _win.type_text(text)
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    preview = text[:40] + ("…" if len(text) > 40 else "")
    return ActionResult(success=True, message=f"已键入：{preview}", detail={"text": text})


def press_key(key: str) -> ActionResult:
    from screen_agent.tools import _win

    vk_map = {
        "enter": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
        "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
        "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    }
    key_l = key.lower().strip()
    vk = vk_map.get(key_l)
    if vk is None and len(key_l) == 1 and key_l.isalnum():
        vk = ord(key_l.upper())
    if vk is None:
        return ActionResult(success=False, message=f"不支持的按键：{key}")
    try:
        _win.press_key(vk)
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    return ActionResult(success=True, message=f"已按 {key}")


def scroll(delta: int) -> ActionResult:
    """delta 正=上滚，负=下滚（单位：格）。"""
    from screen_agent.tools import _win

    try:
        _win.mouse_scroll(delta)
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    return ActionResult(success=True, message=f"已滚动 {delta:+d} 格")
