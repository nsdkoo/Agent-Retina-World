"""语音助手单实例接管：启动时自动关闭上一个实例。

写 PID 锁文件（data/voice_instance.pid）；下次启动读到仍存活的旧 PID
就 Terminate 它并等其退出，再写自己的 PID——避免开一堆终端/重复实例。
"""

from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path

_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _pid_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    exit_code = ctypes.c_ulong()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    return exit_code.value == _STILL_ACTIVE


def _terminate(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    ok = bool(kernel32.TerminateProcess(handle, 1))
    kernel32.CloseHandle(handle)
    return ok


def ensure_single_instance(root: Path, timeout_sec: float = 5.0) -> None:
    """旧实例仍在跑就关掉它（新实例接管）；适用于重启/重复启动场景。"""
    pid_file = root / "data" / "voice_instance.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            old_pid = 0
        me = os.getpid()
        if old_pid and old_pid != me and _pid_alive(old_pid):
            print(f"检测到上一个语音助手实例（PID {old_pid}），正在关闭…")
            _terminate(old_pid)
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline and _pid_alive(old_pid):
                time.sleep(0.1)
            print("已关闭。")

    pid_file.write_text(str(os.getpid()), encoding="utf-8")


def release_instance(root: Path) -> None:
    """干净退出时清掉锁文件（异常退出不清——下次启动照样接管）。"""
    pid_file = root / "data" / "voice_instance.pid"
    try:
        if pid_file.exists():
            pid_file.unlink()
    except OSError:
        pass
