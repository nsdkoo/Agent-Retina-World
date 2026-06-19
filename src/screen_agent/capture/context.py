from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class ForegroundContext:
    window_title: str
    process_name: str | None = None

    @property
    def hint_text(self) -> str:
        parts = [self.window_title]
        if self.process_name:
            parts.append(self.process_name)
        return " ".join(parts).lower()


def get_foreground_context() -> ForegroundContext | None:
    """读取当前前台窗口标题（Windows）。"""
    if sys.platform != "win32":
        return None

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    length = user32.GetWindowTextLengthW(hwnd) + 1
    buf = ctypes.create_unicode_buffer(length)
    user32.GetWindowTextW(hwnd, buf, length)
    title = buf.value.strip()

    process_name: str | None = None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value:
        process_name = _process_name_win(pid.value)

    return ForegroundContext(window_title=title or "(unknown)", process_name=process_name)


def _process_name_win(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None

    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)
    return None
