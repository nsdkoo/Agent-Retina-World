"""系统托盘：显示/隐藏悬浮球、开机自启、退出。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

logger = logging.getLogger(__name__)

AUTOSTART_NAME = "AgentRetinaVoice"


def _make_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#3b82f6"))
    painter.setPen(QColor("#1d4ed8"))
    painter.drawEllipse(6, 6, 52, 52)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(22)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), 0x0084, "AR")  # AlignCenter
    painter.end()
    return QIcon(pixmap)


def _autostart_command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    main_py = Path(__file__).resolve().parents[4] / "main.py"
    return f'"{exe}" "{main_py}" voice'


def is_autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        if enabled:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
        return True
    except OSError as exc:
        logger.warning("设置开机自启失败: %s", exc)
        return False


class TrayIcon(QSystemTrayIcon):
    def __init__(
        self,
        ball_widget,
        on_exit,
        on_toggle_panel=None,
        chat_client=None,
        on_model_switched=None,
        theme_choice: str = "auto",
        on_theme_change=None,
        on_create_shortcut=None,
    ) -> None:
        super().__init__(_make_icon())
        self._ball = ball_widget
        menu = QMenu()

        act_toggle = QAction("显示/隐藏悬浮球", menu)
        act_toggle.triggered.connect(lambda: ball_widget.setVisible(not ball_widget.isVisible()))
        menu.addAction(act_toggle)

        if on_toggle_panel is not None:
            act_panel = QAction("显示输入条（Alt+Space）", menu)
            act_panel.triggered.connect(on_toggle_panel)
            menu.addAction(act_panel)

        menu.addSeparator()

        if chat_client is not None and hasattr(chat_client, "set_preferred"):
            model_menu = menu.addMenu("对话模型")
            from PyQt6.QtGui import QActionGroup

            group = QActionGroup(menu)
            group.setExclusive(True)
            for name in chat_client.backend_names:
                act = QAction(name, model_menu)
                act.setCheckable(True)
                act.setChecked(name == chat_client.current_name)
                act.triggered.connect(
                    lambda _checked, n=name: self._switch_model(n, chat_client, on_model_switched)
                )
                group.addAction(act)
                model_menu.addAction(act)
            menu.addSeparator()

        if on_theme_change is not None:
            from PyQt6.QtGui import QActionGroup

            theme_menu = menu.addMenu("外观")
            theme_group = QActionGroup(menu)
            theme_group.setExclusive(True)
            for label, value in (("跟随系统", "auto"), ("浅色", "light"), ("深色", "dark")):
                act = QAction(label, theme_menu)
                act.setCheckable(True)
                act.setChecked(value == theme_choice)
                act.triggered.connect(lambda _checked, v=value: on_theme_change(v))
                theme_group.addAction(act)
                theme_menu.addAction(act)
            menu.addSeparator()

        self._autostart_action = QAction("开机自启", menu)
        self._autostart_action.setCheckable(True)
        self._autostart_action.setChecked(is_autostart_enabled())
        self._autostart_action.toggled.connect(set_autostart)
        menu.addAction(self._autostart_action)

        menu.addSeparator()

        if on_create_shortcut is not None:
            act_shortcut = QAction("创建桌面快捷方式", menu)
            act_shortcut.triggered.connect(on_create_shortcut)
            menu.addAction(act_shortcut)

        act_exit = QAction("退出", menu)
        act_exit.triggered.connect(on_exit)
        menu.addAction(act_exit)

        self.setContextMenu(menu)
        self.setToolTip("Agent-Retina 语音助手 · 喊「瑞塔」或 Alt+Space")
        self.activated.connect(self._on_activated)

    def _switch_model(self, name: str, chat_client, on_model_switched) -> None:
        ok = chat_client.set_preferred(name)
        if ok and on_model_switched is not None:
            on_model_switched(name)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._ball.setVisible(not self._ball.isVisible())
