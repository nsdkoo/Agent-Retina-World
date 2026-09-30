"""悬浮球：现代极简状态点（小、安静、克制）。

设计取向（2026 语义）：常驻指示器要**小、低存在感、只在有事时亮**——
不是 2000 年代那种亮面玻璃球。所以：
- 尺寸 32px（点本体 ~26px），默认可click可拖，静止时半透明贴着桌面
- 造型极简：细描边 + 轻微通透，无乳白球体、无大高光弧
- 状态只用一圈极细流光表达：聆听跟音量呼吸、思考转弧、播报涟漪
- 全部手绘，不用 QGraphicsEffect（会栅格化发糊）
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QWidget

from screen_agent.voice.ui_qt.signals import AssistantSignals
from screen_agent.voice.ui_qt.toast import BubbleToast

logger = logging.getLogger(__name__)

BALL_SIZE = 36
DOT_RADIUS = 13.0

# 状态 → 呼吸幅度（唯一动效：中心点/圆盘透明度轻呼吸）
STATE_TUNING = {
    "idle": (0.15,),
    "listening": (0.10,),
    "processing": (0.05,),
    "speaking": (0.20,),
    "session": (0.10,),
}

# 状态主色（描边 / 流光环用）
STATE_ACCENTS = {
    "idle": "#9a917f",
    "listening": "#6aa87f",
    "processing": "#b45309",
    "speaking": "#d97706",
    "session": "#6aa87f",
}


class BallWidget(QWidget):
    """极简状态点：小、安静、只在有事时亮。"""

    def __init__(self, signals: AssistantSignals, on_click=None, level_fn=None) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(BALL_SIZE, BALL_SIZE)
        self._signals = signals
        self._on_click = on_click
        self._level_fn = level_fn

        self._status = "idle"
        self._last_tick = time.monotonic()
        self._level = 0.0
        self._hover = 0.0
        self._breathe_phase = 0.0
        self._drag_offset = None
        self._press_pos = None
        self._moved = False
        self.on_moved = None  # 外部注入：拖动时同步气泡位置

        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.right() - BALL_SIZE - 22, geo.top() + geo.height() // 3)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

        self._signals.status.connect(self.set_status)

    def set_status(self, status: str) -> None:
        if status in STATE_TUNING:
            self._status = status

    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last_tick)
        self._last_tick = now
        (breathe,) = STATE_TUNING.get(self._status, STATE_TUNING["idle"])
        if self._level_fn is not None:
            try:
                raw = max(0.0, min(1.0, float(self._level_fn())))
            except Exception:
                raw = 0.0
            self._level = raw * 0.55 + self._level * 0.45
        target_hover = 1.0 if self.underMouse() else 0.0
        self._hover += (target_hover - self._hover) * min(1.0, dt * 10)
        self._breathe_phase = breathe * math.sin(now * 2 * math.pi / 2.4)
        self.update()

    # ---- 绘制：极简状态点 ----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        (breathe,) = STATE_TUNING.get(self._status, STATE_TUNING["idle"])
        active = self._status in ("listening", "processing", "speaking", "session")
        accent = QColor(STATE_ACCENTS.get(self._status, STATE_ACCENTS["idle"]))
        hover = self._hover
        # 轻度呼吸：仅作用于中心点透明度（幅度 ≤15%）
        pulse = breathe * (0.5 + 0.5 * math.sin(time.monotonic() * 2 * math.pi / 2.4))

        cx = cy = BALL_SIZE / 2
        radius = DOT_RADIUS + hover * 0.8

        # 柔和投影（浮起来，不重）
        for i in range(4):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(58, 48, 32, int((6 + i * 4) * (0.6 + hover * 0.3))))
            spread = 4 - i
            painter.drawEllipse(QPointF(cx, cy + 2.0), radius + spread, radius + spread)

        # 扁平圆盘：纯色填充 + 1px 描边（无渐变无高光）
        disc_alpha = 235 + int(hover * 15)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(250, 248, 242, min(255, disc_alpha)))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.setPen(QPen(QColor(150, 138, 112, 200), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # 中心状态点：唯一的状态表达（颜色 + 轻呼吸透明度）
        base_alpha = 150 + int(70 * (1.0 - pulse))
        if not active:
            core = QColor(STATE_ACCENTS["idle"])
        else:
            core = QColor(accent)
        core.setAlpha(min(255, base_alpha))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(cx, cy), 3.2, 3.2)

        painter.end()


    # ---- 交互：拖动 vs 单击 ----

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            self._press_pos = event.globalPosition().toPoint()
            self._moved = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is None:
            return
        new_pos = event.globalPosition().toPoint() - self._drag_offset
        if self._press_pos is not None and (new_pos - (self._press_pos - self._drag_offset)).manhattanLength() > 6:
            self._moved = True
        self.move(new_pos)
        if self.on_moved is not None:
            self.on_moved()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            was_drag = self._moved
            self._drag_offset = None
            self._press_pos = None
            if not was_drag and self._on_click:
                self._on_click()



def _ui_state_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "ui_state.json"


def load_ui_state() -> dict:
    try:
        return json.loads(_ui_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_ui_state(**kwargs) -> None:
    path = _ui_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = load_ui_state()
        data.update(kwargs)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("保存界面状态失败", exc_info=True)


def load_preferred_model() -> str | None:
    return str(load_ui_state().get("chat_backend") or "") or None


def save_preferred_model(name: str) -> None:
    save_ui_state(chat_backend=name)


def detect_system_theme() -> str:
    """Windows 应用主题：1=浅色，0=深色。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) == 1 else "dark"
    except Exception:
        return "light"


def resolve_theme(choice: str) -> str:
    if choice in ("light", "dark"):
        return choice
    return detect_system_theme()


class QtFloatingBall:
    """总装：悬浮球 + 输入条 + 托盘 + 全局热键 + 语音助手后台线程。"""

    FADE_MS = 150

    def __init__(self, assistant) -> None:
        self.assistant = assistant
        self._panel = None
        self._panel_anim = None
        self._hotkey = None
        self._toast = None

    # ---- 全局热键 Alt+Space（ChatGPT 桌面同款唤出方式）----

    def _install_hotkey(self, app, callback) -> None:
        import ctypes
        from ctypes import wintypes

        from PyQt6.QtCore import QAbstractNativeEventFilter

        VK_SPACE, MOD_ALT, WM_HOTKEY = 0x20, 0x0001, 0x0312

        class _MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG),
            ]

        class _Filter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, event_type, message):  # noqa: N802
                if event_type == b"windows_generic_MSG":
                    msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
                    if msg.message == WM_HOTKEY:
                        callback()
                        return True, 0
                return False, 0

        try:
            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, 1, MOD_ALT, VK_SPACE):
                logger.warning("全局热键 Alt+Space 注册失败（可能被占用）")
                return
            self._hotkey_filter = _Filter()
            app.installNativeEventFilter(self._hotkey_filter)
            self._hotkey = (user32, 1)
        except Exception:
            logger.warning("全局热键注册异常", exc_info=True)

    def _uninstall_hotkey(self) -> None:
        if self._hotkey is not None:
            user32, hotkey_id = self._hotkey
            try:
                user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass
            self._hotkey = None

    def _recent_activity(self) -> str:
        try:
            md = self.assistant.pipeline.proactive.timeline_markdown(limit=3)
            lines = [ln.strip() for ln in md.split("\n") if ln.startswith("- ")]
            return "\n".join(lines[:3])
        except Exception:
            return ""

    def run(self) -> None:
        import threading

        from PyQt6.QtCore import QPropertyAnimation, QTimer
        from PyQt6.QtWidgets import QApplication

        from screen_agent.voice.ui_qt.panel import ChatPanel
        from screen_agent.voice.ui_qt.tray import TrayIcon

        app = QApplication.instance() or QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)

        signals = AssistantSignals()

        level_fn = getattr(self.assistant.audio_loop, "get_level", None)
        chat_client = getattr(self.assistant.executor, "chat_client", None)
        switchable = hasattr(chat_client, "set_preferred")
        if switchable:
            saved = load_preferred_model()
            if saved:
                chat_client.set_preferred(saved)

        state = load_ui_state()
        theme_choice = str(state.get("theme") or "auto")  # auto | light | dark
        wake_hint = "喊「瑞塔」"
        try:
            chinese = [w for w in self.assistant.wake_names if not w.isascii()]
            if chinese:
                wake_hint = f"喊「{chinese[0]}」"
        except Exception:
            pass

        def on_model_change(name: str) -> None:
            if switchable:
                chat_client.set_preferred(name)
                save_preferred_model(name)
            msg = f"对话模型已切换 → {name}"
            if self._panel is not None:
                self._panel.add_info(msg)
            self.assistant.speak(msg)

        def on_theme_change(choice: str) -> None:
            save_ui_state(theme=choice)
            resolved = resolve_theme(choice)
            if self._panel is not None:
                self._panel.set_theme(resolved)
            if self._toast is not None:
                self._toast.set_theme(resolved)

        panel = ChatPanel(
            signals,
            on_submit_text=self._submit_text,
            activity_fn=self._recent_activity,
            on_height_changed=self._reposition_panel,
            model_options=chat_client.backend_names if switchable else None,
            current_model=chat_client.current_name if switchable else "",
            on_model_change=on_model_change,
            theme=resolve_theme(theme_choice),
            wake_hint=wake_hint,
        )
        panel.setWindowOpacity(0.0)
        if switchable:
            panel.set_model_label(chat_client.current_name)
        self._panel = panel
        self._ball = None
        self._panel_anim = QPropertyAnimation(panel, b"windowOpacity", panel)
        self._panel_anim.setDuration(self.FADE_MS)
        ball = BallWidget(signals, on_click=lambda: self._toggle_panel(panel, ball), level_fn=level_fn)
        self._ball = ball

        # ---- 会话气泡：AI 回话自动浮现，不用点开面板 ----
        toast = BubbleToast(resolve_theme(theme_choice), on_click=lambda: self._toggle_panel(panel, ball))
        self._toast = toast

        def ball_anchor() -> QPointF:
            center = ball.geometry().center()
            return QPointF(center.x() + ball.width() / 2, center.y())

        def sync_toast() -> None:
            toast.set_anchor(ball_anchor())

        ball.on_moved = sync_toast

        def on_transcript(text: str) -> None:
            signals.transcript.emit(text)
            toast.show_message(text, "User", timeout_ms=6000, anchor=ball_anchor())

        def on_result(text: str) -> None:
            signals.result.emit(text)
            # 常驻到播报结束（play_end 再给 3.5s 收尾），不打断你当下的视线
            toast.show_message(text, "Bot", timeout_ms=None, anchor=ball_anchor())

        def on_delta(accumulated: str) -> None:
            signals.partial.emit(accumulated)
            if accumulated:
                toast.show_message(accumulated, "Bot", timeout_ms=None, anchor=ball_anchor())

        def on_options(question: str, options: list) -> None:
            signals.prompt.emit(question, list(options))
            # 需要用户拍板时把面板拉出来，否则按钮摆在那儿没人点得到
            if self._panel is not None and not self._panel.isVisible():
                self._toggle_panel(self._panel, self._ball)

        def on_suggestion(items: list) -> None:
            """主动建议：用气泡说一句，**点一下就能执行**。

            **刻意不语音播报** —— 主动说的话如果还念出来会很吵，
            用户正专注时尤其烦。文字扫一眼就够，想看再看。
            """
            if not items:
                return
            top = items[0]
            signals.suggestion.emit([s.to_dict() for s in items])

            def _run() -> None:
                """气泡被点击 —— 这是「建议」和「提示」的分水岭。

                提示只能看，建议点一下就能动手。执行走的是常规通道
                （照样过权限审批），不是特权路径。
                """
                result = self.assistant.run_suggestion(top)
                toast.hide_now()
                if result is not None and getattr(result, "message", ""):
                    toast.show_message(result.message, "Bot", timeout_ms=6000,
                                       anchor=ball_anchor())

            clickable = getattr(top, "action", None)
            hint = "　（点一下执行）" if clickable else ""
            toast.show_message(top.render() + hint, "Rita", timeout_ms=8000,
                               anchor=ball_anchor(),
                               on_click=_run if clickable else None)

        self.assistant.on_transcript(on_transcript)
        self.assistant.on_result(on_result)
        self.assistant.on_result_delta(on_delta)
        self.assistant.on_options(on_options)
        self.assistant.on_progress(signals.progress.emit)
        self.assistant.on_suggestion(on_suggestion)

        def play_start_chain() -> None:
            self.assistant.audio_loop.set_muted_mic()
            signals.status.emit("speaking")

        def play_end_chain() -> None:
            self.assistant.audio_loop.set_unmuted_mic()
            signals.status.emit("session" if self.assistant.in_session else "idle")
            if self._toast is not None:
                self._toast.set_timeout(3500)

        self.assistant.speaker.on_play_start = play_start_chain
        self.assistant.speaker.on_play_end = play_end_chain

        # 语音线程 → UI 线程（transcript / result 已在上面接成"面板 + 气泡"双通道）
        self.assistant.on_status(signals.status.emit)
        self.assistant.on_session(signals.session.emit)

        def on_exit() -> None:
            if self._toast is not None:
                self._toast.hide_now()
            self._uninstall_hotkey()
            self.assistant.stop()
            QTimer.singleShot(300, app.quit)

        # 托盘右键也能切模型（面板上点模型名是主入口）
        def on_model_switched(name: str) -> None:
            panel.set_model_label(name)
            on_model_change(name)

        def _create_shortcut_notify() -> None:
            from screen_agent.voice.shortcuts import create_shortcuts
            try:
                lines = create_shortcuts(self.assistant.project_root)
                tray.showMessage("瑞塔", chr(10).join(lines), QSystemTrayIcon.MessageIcon.Information, 3000)
            except Exception as exc:
                tray.showMessage("瑞塔", f"创建失败：{exc}", QSystemTrayIcon.MessageIcon.Critical, 4000)

        tray = TrayIcon(
            ball,
            on_exit=on_exit,
            on_toggle_panel=lambda: self._toggle_panel(panel, ball),
            chat_client=chat_client if switchable else None,
            on_model_switched=on_model_switched if switchable else None,
            theme_choice=theme_choice,
            on_theme_change=on_theme_change,
            on_create_shortcut=_create_shortcut_notify,
        )
        tray.show()
        ball.show()

        # 全局热键 Alt+Space 唤出/收起输入条（与 ChatGPT 桌面伴侣窗同款）
        self._install_hotkey(app, lambda: self._toggle_panel(panel, ball))

        # 面板不随焦点自动收起：Esc / ✕ / Alt+Space / 再点球关闭，打字中途不会消失

        self.assistant.run_in_background()
        app.exec()

    def _fade_panel(self, show: bool) -> None:
        from PyQt6.QtCore import QPropertyAnimation, QEasingCurve

        self._panel_anim.stop()
        self._panel_anim.setStartValue(self._panel.windowOpacity())
        self._panel_anim.setEndValue(1.0 if show else 0.0)
        self._panel_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        try:
            self._panel_anim.finished.disconnect()
        except TypeError:
            pass
        if show:
            self._panel_anim.finished.connect(self._panel.show)
            self._panel.show()
            self._panel.raise_()
        else:
            self._panel_anim.finished.connect(self._panel.hide)
        self._panel_anim.start()

    def _panel_anchor(self) -> tuple[int, int]:
        """面板在状态点左侧、底部与点对齐：输入框贴着点（点击后输入自然在手边）。"""
        ball, panel = self._ball, self._panel
        geo = ball.geometry()
        screen = ball.screen() or panel.screen()
        avail = screen.availableGeometry() if screen is not None else None

        x = geo.left() - panel.width() + 16
        if avail is not None and x < avail.left() + 8:
            x = geo.right() - 16
        y = geo.bottom() - panel.height() + 10
        if avail is not None:
            y = max(y, avail.top() + 8)
            x = min(max(x, avail.left() + 8), avail.right() - panel.width() - 8)
        return x, y

    def _reposition_panel(self) -> None:
        if self._panel is None or not self._panel.isVisible():
            return
        x, y = self._panel_anchor()
        self._panel.move(x, y)

    def _toggle_panel(self, panel, ball) -> None:
        if panel.isVisible():
            self._fade_panel(False)
            return
        self._panel, self._ball = panel, ball
        x, y = self._panel_anchor()
        panel.move(x, y)
        self._fade_panel(True)
        panel.focus_input()

    def _submit_text(self, text: str) -> None:
        """打字输入：与语音共用 handle_command，跑在独立线程。"""
        import threading

        def job() -> None:
            try:
                self.assistant.set_status("processing")
                result = self.assistant.handle_command(text)
                if result is not None:
                    self.assistant.emit_result(result.message)
                    self.assistant.speak(result.message)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("文字指令执行失败")
            finally:
                # 异常路径兜底：状态复位，避免球永久卡在 processing
                self.assistant.set_status("session" if self.assistant.in_session else "idle")

        threading.Thread(target=job, daemon=True).start()
