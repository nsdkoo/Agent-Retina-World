"""默认工具注册表：把全部能力模块装配成 ToolRegistry。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from screen_agent.tools import clipboard, dialog, files, input, system, volume, windows
from screen_agent.tools.apps import AppResolver
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec
from screen_agent.voice.executor import ActionResult


def build_default_registry(
    app_resolver: AppResolver | None = None,
    confirm_fn: Callable[[ToolSpec, dict], bool] | None = None,
) -> ToolRegistry:
    resolver = app_resolver or AppResolver(
        Path(__file__).resolve().parents[3] / "data" / "cache" / "app_index.json"
    )
    registry = ToolRegistry(confirm_fn=confirm_fn)
    register = registry.register

    # ---- 应用 ----
    def _app_open(target: str) -> ActionResult:
        resolved = resolver.resolve(target)
        if resolved is None:
            return ActionResult(success=False, message=f"没找到「{target}」这个应用")
        resolver.launch(resolved)
        return ActionResult(success=True, message=f"正在打开 {target}", detail={"app": resolved.target})

    def _app_close(target: str) -> ActionResult:
        return windows.close_app_safe(target)

    def _app_list() -> ActionResult:
        names = sorted({lnk.stem for lnk in resolver._collect_lnk_files()})
        preview = "、".join(names[:40])
        return ActionResult(success=True, message=f"开始菜单应用（前 40）：{preview}", detail={"count": len(names)})

    register(ToolSpec("app.open", "打开应用", _app_open, RiskLevel.LOW, {"target": "应用名或路径"}))
    register(ToolSpec("app.close", "关闭应用", _app_close, RiskLevel.HIGH, {"target": "进程名"}))
    register(ToolSpec("app.list", "列出已装应用", _app_list, RiskLevel.SAFE))

    # ---- 文件（读）----
    register(ToolSpec("files.open", "打开文件或文件夹", files.open_path, RiskLevel.LOW, {"path": "路径"}))
    register(ToolSpec("files.list", "列出目录内容", files.list_dir, RiskLevel.SAFE, {"path": "目录"}))
    register(ToolSpec("files.find", "模糊找文件", files.find_files, RiskLevel.SAFE, {"pattern": "文件名关键词"}))

    # ---- 文件（写）——白名单 + 重名不覆盖 + 操作日志，delete 走回收站且需确认 ----
    register(ToolSpec("files.mkdir", "新建文件夹", files.make_dir, RiskLevel.LOW, {"path": "文件夹名或路径"}))
    register(ToolSpec("files.move", "移动文件到目标目录", files.move_path, RiskLevel.LOW,
                      {"src": "源路径", "dst": "目标目录或完整路径"}))
    register(ToolSpec("files.copy", "复制文件到目标目录", files.copy_path, RiskLevel.LOW,
                      {"src": "源路径", "dst": "目标目录或完整路径"}))
    register(ToolSpec("files.rename", "重命名文件", files.rename_path, RiskLevel.LOW,
                      {"src": "源路径", "new_name": "新名字"}))
    register(ToolSpec("files.organize", "把目录里的文件按类型或修改时间归档进子文件夹（先出清单再动手）",
                      files.organize_dir, RiskLevel.LOW,
                      {"path": "目录，默认桌面", "mode": "type 按类型 / date 按时间 / 留空则先问用户"}))
    register(ToolSpec("files.delete", "删除文件（放进回收站，可还原）", files.delete_path,
                      RiskLevel.HIGH, {"path": "路径"}))
    register(ToolSpec("files.undo", "撤销上一次文件操作", files.undo_last, RiskLevel.LOW))

    # ---- 交互：把需要用户拍板的事变成可点选项 ----
    register(ToolSpec("ask.options", "需要用户拍板时提问并给出候选，别自己替用户猜",
                      dialog.ask_options, RiskLevel.SAFE,
                      {"question": "要问用户的问题", "options": "候选，用 | 分隔，最多 4 个"}))

    # ---- 剪贴板 ----
    register(ToolSpec("clip.get", "读剪贴板", lambda: clipboard.clip_get_text(), RiskLevel.SAFE))
    register(ToolSpec("clip.set", "写剪贴板", clipboard.clip_set_text, RiskLevel.LOW, {"text": "内容"}))

    # ---- 窗口 ----
    register(ToolSpec("win.list", "列出窗口", lambda: windows.list_windows(), RiskLevel.SAFE))
    register(ToolSpec("win.focus", "聚焦窗口", windows.focus_window, RiskLevel.LOW, {"title": "标题关键词"}))

    # ---- 输入（GUI 操控原语，HIGH）----
    register(ToolSpec("input.mouse_move", "移动鼠标", input.mouse_move, RiskLevel.HIGH, {"x": "横坐标", "y": "纵坐标"}))
    register(ToolSpec("input.mouse_click", "点击鼠标", input.mouse_click, RiskLevel.HIGH,
                      {"x": "横坐标", "y": "纵坐标", "button": "left/right/middle", "double": "是否双击"}))
    register(ToolSpec("input.type_text", "键入文字", input.type_text, RiskLevel.HIGH, {"text": "内容"}))
    register(ToolSpec("input.press_key", "按键", input.press_key, RiskLevel.HIGH, {"key": "键名 enter/esc/tab/字母"}))
    register(ToolSpec("input.scroll", "滚轮", input.scroll, RiskLevel.HIGH, {"delta": "格数，正上负下"}))

    # ---- 音量 ----
    register(ToolSpec("volume.up", "音量调大", volume.volume_up, RiskLevel.LOW, {"steps": "步数"}))
    register(ToolSpec("volume.down", "音量调小", volume.volume_down, RiskLevel.LOW, {"steps": "步数"}))
    register(ToolSpec("volume.mute", "静音切换", lambda: volume.volume_mute(), RiskLevel.LOW))
    register(ToolSpec("volume.get", "读音量", lambda: volume.volume_get(), RiskLevel.SAFE))

    # ---- 系统 ----
    register(ToolSpec("sys.lock", "锁屏", lambda: system.lock_screen(), RiskLevel.HIGH))
    register(ToolSpec("sys.screenshot_clip", "截屏到剪贴板", lambda: system.screenshot_to_clipboard(), RiskLevel.LOW))
    register(ToolSpec("sys.open_url", "打开网页", system.open_url, RiskLevel.LOW, {"url": "网址"}))

    return registry
