"""快捷方式创建：桌面 + 开始菜单（WScript.Shell，pythonw 静默启动无黑窗）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _ps_quote(text: str) -> str:
    return text.replace("'", "''")


def generate_icon(icon_path: Path) -> Path:
    """生成扁平风格图标：燕麦白圆盘 + 金棕中心点（与悬浮球同款视觉）。"""
    from PIL import Image, ImageDraw

    icon_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([10, 10, 246, 246], fill=(250, 248, 242, 255), outline=(150, 138, 112, 255), width=8)
    draw.ellipse([114, 114, 142, 142], fill=(180, 83, 9, 255))
    img.save(icon_path, sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)])
    return icon_path


def _create_lnk(lnk_path: Path, target: str, args: str, workdir: str, icon: str) -> None:
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('"
        + _ps_quote(str(lnk_path)) + "');"
        "$s.TargetPath = '" + _ps_quote(target) + "';"
        "$s.Arguments = '" + _ps_quote(args) + "';"
        "$s.WorkingDirectory = '" + _ps_quote(workdir) + "';"
        "$s.IconLocation = '" + _ps_quote(icon) + "';"
        "$s.Save()"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"创建快捷方式失败：{(proc.stderr or '').strip()[:120]}")


def _shell_folder(folder: str) -> Path:
    """经 shell API 取真实路径（兼容 OneDrive 重定向的桌面）。"""
    script = f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Environment]::GetFolderPath('{folder}')"
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    return Path(proc.stdout.strip() or str(Path.home() / folder))


def create_shortcuts(project_root: Path) -> list[str]:
    """创建桌面 + 开始菜单快捷方式，返回创建的路径列表。"""
    project_root = project_root.resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)  # 兜底（有控制台窗口）
    icon = project_root / "assets" / "rita.ico"
    generate_icon(icon)

    created: list[str] = []
    targets = [
        (_shell_folder("Desktop") / "瑞塔语音助手.lnk", "桌面"),
        (
            _shell_folder("ApplicationData") / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "瑞塔语音助手.lnk",
            "开始菜单",
        ),
    ]
    for lnk_path, label in targets:
        _create_lnk(
            lnk_path,
            target=str(pythonw),
            args="main.py voice",
            workdir=str(project_root),
            icon=str(icon),
        )
        created.append(f"{label}：{lnk_path}")
    return created
