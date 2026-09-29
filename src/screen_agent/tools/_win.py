"""ctypes 裸 API 封装：SendInput / 窗口枚举 / 剪贴板 / 前台窗口。

只放无业务语义的底层调用；非 Windows 平台抛 PlatformError。
坐标换算常量：SendInput 绝对坐标归一化到 0~65535。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from screen_agent.tools.registry import PlatformError

if sys.platform != "win32":
    raise PlatformError("screen_agent.tools._win 仅支持 Windows")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---- 常量 ----
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYDOWN = 0x0000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
SM_CXSCREEN = 0
SM_CYSCREEN = 1
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WHEEL_DELTA = 120


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUTunion),
    ]


def screen_size() -> tuple[int, int]:
    return (
        user32.GetSystemMetrics(SM_CXSCREEN),
        user32.GetSystemMetrics(SM_CYSCREEN),
    )


def _send_input(*inputs: INPUT) -> None:
    sent = user32.SendInput(len(inputs), (INPUT * len(inputs))(*inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise PlatformError(f"SendInput 只注入了 {sent}/{len(inputs)} 个事件")


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, mouse_data: int = 0) -> INPUT:
    item = INPUT(type=INPUT_MOUSE)
    item.union.mi = _MOUSEINPUT(dx, dy, mouse_data, flags, 0, None)
    return item


def _key_input(flags: int, *, vk: int = 0, scan: int = 0) -> INPUT:
    item = INPUT(type=INPUT_KEYBOARD)
    item.union.ki = _KEYBDINPUT(vk, scan, flags, 0, None)
    return item


# ---- 鼠标 ----

def mouse_move_abs(x: int, y: int) -> None:
    sw, sh = screen_size()
    if sw <= 0 or sh <= 0:
        raise PlatformError("无法获取屏幕尺寸")
    nx = max(0, min(sw - 1, int(x))) * 65535 // max(1, sw - 1)
    ny = max(0, min(sh - 1, int(y))) * 65535 // max(1, sh - 1)
    _send_input(_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny))


def mouse_click(button: str = "left", double: bool = False) -> None:
    pairs = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    down, up = pairs.get(button, pairs["left"])
    clicks = 2 if double else 1
    for _ in range(clicks):
        _send_input(_mouse_input(down), _mouse_input(up))
        if double:
            time.sleep(0.05)


def mouse_scroll(delta_clicks: int) -> None:
    _send_input(_mouse_input(MOUSEEVENTF_WHEEL, mouse_data=delta_clicks * WHEEL_DELTA))


# ---- 键盘 ----

def press_key(vk: int) -> None:
    _send_input(_key_input(KEYEVENTF_KEYDOWN, vk=vk), _key_input(KEYEVENTF_KEYUP, vk=vk))


def type_text(text: str) -> None:
    """中文/任意 Unicode 逐字符注入（KEYEVENTF_UNICODE）。"""
    for ch in text:
        scan = ord(ch)
        _send_input(
            _key_input(KEYEVENTF_UNICODE, scan=scan),
            _key_input(KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, scan=scan),
        )


# ---- 窗口 ----

def list_windows() -> list[dict[str, str]]:
    """可见顶层窗口：[{hwnd, title, process}]。"""
    results: list[dict[str, str]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _on_window(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append(
            {"hwnd": str(int(hwnd)), "title": buf.value, "pid": str(pid.value)}
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_on_window), 0)
    return results


def focus_window(hwnd: int) -> bool:
    """前台切换：AttachThreadInput 规避前台锁 + ALT 键 trick 双保险。"""
    foreground = user32.GetForegroundWindow()
    if int(foreground) == hwnd:
        return True
    cur_tid = kernel32.GetCurrentThreadId()
    other_tid = user32.GetWindowThreadProcessId(foreground, None)
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    if other_tid and other_tid != cur_tid:
        user32.AttachThreadInput(cur_tid, other_tid, True)
    if target_tid and target_tid != cur_tid:
        user32.AttachThreadInput(cur_tid, target_tid, True)
    user32.keybd_event(0x12, 0, 0, 0)  # ALT down（解除前台锁）
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)  # ALT up（KEYEVENTF_KEYUP）
    if other_tid and other_tid != cur_tid:
        user32.AttachThreadInput(cur_tid, other_tid, False)
    if target_tid and target_tid != cur_tid:
        user32.AttachThreadInput(cur_tid, target_tid, False)
    return int(user32.GetForegroundWindow()) == hwnd


# ---- 剪贴板（CF_UNICODETEXT）----

def _open_clipboard(retries: int = 3) -> None:
    for attempt in range(retries):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.01 * (attempt + 1))
    raise PlatformError("剪贴板被其他程序占用")


def clipboard_get_text() -> str:
    _open_clipboard()
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return ""
        try:
            return ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_set_text(text: str) -> None:
    _open_clipboard()
    try:
        user32.EmptyClipboard()
        size = (len(text) + 1) * 2  # UTF-16 + NUL
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise PlatformError("GlobalAlloc 失败")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise PlatformError("GlobalLock 失败")
        ctypes.memmove(locked, ctypes.create_unicode_buffer(text), size)
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)  # 所有权移交系统
    finally:
        user32.CloseClipboard()
