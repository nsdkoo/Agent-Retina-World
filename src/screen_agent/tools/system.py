"""系统操作：锁屏 / 截屏入剪贴板 / 打开网页。"""

from __future__ import annotations

import webbrowser

from screen_agent.voice.executor import ActionResult


def lock_screen() -> ActionResult:
    import ctypes

    ctypes.windll.user32.LockWorkStation()
    return ActionResult(success=True, message="已锁屏")


def screenshot_to_clipboard() -> ActionResult:
    """截全屏到剪贴板（GDI BitBlt → CF_BITMAP，或 PIL ImageGrab 优先）。"""
    try:
        from PIL import ImageGrab  # type: ignore

        image = ImageGrab.grab()
        import io

        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "BMP")
        data = buffer.getvalue()[14:]  # 去掉 BMP 文件头，剪贴板要 DIB
        _set_clipboard_dib(data)
        return ActionResult(success=True, message="已截全屏到剪贴板，可直接粘贴")
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        return ActionResult(success=False, message=f"截屏失败：{exc}")
    return ActionResult(success=False, message="截屏需要 Pillow（pip install pillow）或手动配置 DIB")


def _set_clipboard_dib(dib_data: bytes) -> None:
    import ctypes

    from screen_agent.tools import _win

    user32 = _win.user32
    kernel32 = _win.kernel32
    CF_DIB = 8
    _win._open_clipboard()
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(_win.GMEM_MOVEABLE, len(dib_data))
        if not handle:
            raise PlatformError("GlobalAlloc 失败")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise PlatformError("GlobalLock 失败")
        ctypes.memmove(locked, dib_data, len(dib_data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_DIB, handle)
    finally:
        user32.CloseClipboard()


def open_url(url: str) -> ActionResult:
    if not url:
        return ActionResult(success=False, message="网址是空的")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(success=False, message=f"打开网页失败：{exc}")
    return ActionResult(success=True, message=f"已打开网页：{url[:50]}", detail={"url": url})
