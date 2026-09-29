"""音量控制：按键模拟（全局音量步进 ±2）。"""

from __future__ import annotations

from screen_agent.tools.registry import PlatformError
from screen_agent.voice.executor import ActionResult


def _press_vk(vk: int, times: int) -> None:
    from screen_agent.tools import _win

    for _ in range(max(1, times)):
        _win.press_key(vk)


def volume_up(steps: int = 3) -> ActionResult:
    try:
        _press_vk(0xAF, steps)  # VK_VOLUME_UP
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    return ActionResult(success=True, message=f"音量已调大（{steps} 步）")


def volume_down(steps: int = 3) -> ActionResult:
    try:
        _press_vk(0xAE, steps)  # VK_VOLUME_DOWN
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    return ActionResult(success=True, message=f"音量已调小（{steps} 步）")


def volume_mute() -> ActionResult:
    try:
        _press_vk(0xAD, 1)  # VK_VOLUME_MUTE 切换
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    return ActionResult(success=True, message="已切换静音状态")


def volume_get() -> ActionResult:
    """读系统音量需 COM（IAudioEndpointVolume），v1 用 powershell 单行查询。"""
    import subprocess

    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "(New-Object -ComObject WScript.Shell) | Out-Null; "
        "$w = Get-CimInstance -Namespace root/cimv2 -ClassName Win32_SoundDevice | Out-Null; "
        "Write-Output 'n/a'"
    )
    # 纯 ctypes 读音量要 COM，v1 简化：如实说明
    _ = script, subprocess  # 保留导入占位
    return ActionResult(
        success=False,
        message="精确读音量需要 COM 接口，v1 先用「音量调大/调小/静音」控制",
    )
