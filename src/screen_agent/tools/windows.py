"""窗口工具：列可见窗口 / 前台聚焦。"""

from __future__ import annotations

from screen_agent.tools.registry import PlatformError
from screen_agent.voice.executor import ActionResult

# 系统进程白名单：close/list 场景不碰
_SYSTEM_PROCESSES = {"explorer", "winlogon", "csrss", "dwm", "system", "registry"}

# 中文名 → 实际 exe 名（国产应用 exe 多为英文）
_COMMON_EXE = {
    "微信": "WeChat",
    "企业微信": "WXWork",
    "qq": "QQ",
    "钉钉": "DingTalk",
    "网易云音乐": "cloudmusic",
    "QQ音乐": "QQMusic",
    "飞书": "Feishu",
    "抖音": "douyin",
    "百度网盘": "BaiduNetdisk",
}


def list_windows() -> ActionResult:
    from screen_agent.tools import _win

    try:
        windows = _win.list_windows()
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    if not windows:
        return ActionResult(success=False, message="没有可见窗口")
    lines = [f"- {w['title'][:50]}" for w in windows[:15]]
    more = f"\n（共 {len(windows)} 个）" if len(windows) > 15 else ""
    return ActionResult(
        success=True, message="当前窗口：\n" + "\n".join(lines) + more, detail={"windows": windows[:15]}
    )


def focus_window(title: str) -> ActionResult:
    from screen_agent.tools import _win

    if not title:
        return ActionResult(success=False, message="告诉我窗口标题关键词")
    try:
        windows = _win.list_windows()
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    needle = title.lower()
    matches = [w for w in windows if needle in w["title"].lower()]
    if not matches:
        return ActionResult(success=False, message=f"没找到标题含「{title}」的窗口")
    target = matches[0]
    try:
        _win.focus_window(int(target["hwnd"]))
    except PlatformError as exc:
        return ActionResult(success=False, message=str(exc))
    return ActionResult(success=True, message=f"已切到：{target['title'][:40]}", detail=target)


def close_app_safe(exe_name: str) -> ActionResult:
    """关闭应用（taskkill），系统进程白名单硬拒绝，中文名自动映射 exe。"""
    import subprocess

    mapped = _COMMON_EXE.get(exe_name.strip(), exe_name)
    stem = mapped.lower().removesuffix(".exe")
    if stem in _SYSTEM_PROCESSES:
        return ActionResult(success=False, message=f"「{exe_name}」是系统进程，拒绝关闭")
    try:
        proc = subprocess.run(
            ["taskkill", "/IM", f"{stem}.exe", "/F"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ActionResult(success=False, message=f"关闭失败：{exc}")
    if proc.returncode != 0:
        return ActionResult(success=False, message=f"没在运行或关闭失败：{(proc.stderr or '').strip()[:80]}")
    return ActionResult(success=True, message=f"已关闭 {exe_name}")
