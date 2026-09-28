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
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget

from screen_agent.voice.ui_qt.signals import AssistantSignals
from screen_agent.voice.ui_qt.toast import BubbleToast

logger = logging.getLogger(__name__)

BALL_SIZE = 36
DOT_RADIUS = 13.0

# Apple Intelligence 流光配色（固定顺序，勿打乱）
AURORA = ["#0894FF", "#C959DD", "#FF2E54", "#FF9004"]

# 状态 → (转速 deg/s, 强度 0-1, 呼吸幅度)
STATE_TUNING = {
    "idle": (40, 0.40, 0.15),
    "listening": (130, 1.00, 0.10),
    "processing": (220, 0.85, 0.05),
    "speaking": (130, 0.95, 0.20),
    "session": (130, 1.00, 0.10),
}

# 状态主色（描边 / 流光环用）
STATE_ACCENTS = {
    "idle": "#9a917f",
    "listening": "#6aa87f",
    "processing": "#c96442",
    "speaking": "#d97757",
    "session": "#6aa87f",
}


def _aurora_gradient(cx: float, cy: float, angle: float):  # noqa: ANN201 - 保留给后续视觉扩展
    from PyQt6.QtGui import QConicalGradient

    grad = QConicalGradient(cx, cy, angle)
    for i, hex_color in enumerate(AURORA):
        grad.setColorAt(i / len(AURORA), QColor(hex_color))
    grad.setColorAt(1.0, QColor(AURORA[0]))
    return grad


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
        self._angle = 0.0
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
        speed, _intensity, breathe = STATE_TUNING.get(self._status, STATE_TUNING["idle"])
        self._angle = (self._angle + speed * dt) % 360.0
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

        _speed, intensity, _breathe = STATE_TUNING.get(self._status, STATE_TUNING["idle"])
        active = self._status in ("listening", "processing", "speaking", "session")
        level = self._level if active else 0.0
        accent = QColor(STATE_ACCENTS.get(self._status, STATE_ACCENTS["idle"]))
        hover = self._hover
        now = time.monotonic()

        cx = cy = BALL_SIZE / 2
        radius = DOT_RADIUS + hover * 0.8
        # 安静：静止时半透明，悬停/活动时提亮（始终保留通透感）
        base_alpha = 0.52 + hover * 0.34 + (0.24 if active else 0.0)

        # 0. 柔和投影（让玻璃点浮起来，但不重）
        for i in range(4):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(20, 30, 48, int((7 + i * 4) * min(1.0, base_alpha))))
            spread = 4 - i
            painter.drawEllipse(QPointF(cx, cy + 2.0), radius + spread, radius + spread)

        # 1. 本体：通透磨砂圆（细腻三层，无亮面高光球）
        grad = QRadialGradient(cx - radius * 0.35, cy - radius * 0.45, radius * 2.0)
        grad.setColorAt(0.0, QColor(255, 255, 255, int(238 * min(1.0, base_alpha))))
        if active:
            tint = QColor(accent)
            tint.setAlpha(int(58 * min(1.0, base_alpha)))
            grad.setColorAt(0.5, tint)
            grad.setColorAt(0.85, QColor(accent.red() // 3 + 150, accent.green() // 3 + 160, accent.blue() // 3 + 170, int(190 * min(1.0, base_alpha))))
        else:
            grad.setColorAt(0.5, QColor(240, 245, 252, int(206 * min(1.0, base_alpha))))
            grad.setColorAt(0.85, QColor(199, 209, 224, int(190 * min(1.0, base_alpha))))
        grad.setColorAt(1.0, QColor(158, 172, 195, int(170 * min(1.0, base_alpha))))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(grad)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # 2. 边缘：细描边 + 内侧一圈极淡反光（Liquid Glass 的"暗边+refraction"极简化版）
        edge = QColor(38, 52, 72, int(78 + 46 * min(1.0, base_alpha)))
        painter.setPen(QPen(edge, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        inner_hi = QColor(255, 255, 255, int(70 * min(1.0, base_alpha)))
        painter.setPen(QPen(inner_hi, 1.0))
        painter.drawEllipse(QPointF(cx, cy), radius - 1.4, radius - 1.4)

        # 空闲：中心一颗小点，表明"我在"，但不抢眼
        if not active:
            core = QColor(STATE_ACCENTS["idle"])
            core.setAlpha(int(120 + 80 * min(1.0, base_alpha)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(core)
            painter.drawEllipse(QPointF(cx, cy), 2.9 + hover * 0.8, 2.9 + hover * 0.8)

        # 2. 状态表达：一圈极细流光
        if self._status in ("listening", "session"):
            ring = QColor(accent)
            ring.setAlpha(int(90 + 110 * min(1.0, level * 2)))
            painter.setPen(QPen(ring, 1.3))
            rr = radius + 2.8 + self._breathe_phase * 1.4 + level * 3.2
            painter.drawEllipse(QPointF(cx, cy), rr, rr)
        elif self._status == "processing":
            arc_color = QColor(accent)
            arc_color.setAlpha(220)
            painter.setPen(QPen(arc_color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            halo = QRectF(cx - radius - 3.0, cy - radius - 3.0, (radius + 3.0) * 2, (radius + 3.0) * 2)
            painter.drawArc(halo, int((self._angle * 3) % 360) * 16, 110 * 16)
        elif self._status == "speaking":
            for i in range(2):
                phase = (now * 1.05 + i * 0.5) % 1.0
                ripple = QColor(accent)
                ripple.setAlpha(int((1.0 - phase) * 80))
                painter.setPen(QPen(ripple, 1.3))
                rr = radius + 2.6 + phase * 6.5
                painter.drawEllipse(QPointF(cx, cy), rr, rr)

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

        self.assistant.on_transcript(on_transcript)
        self.assistant.on_result(on_result)

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

        tray = TrayIcon(
            ball,
            on_exit=on_exit,
            on_toggle_panel=lambda: self._toggle_panel(panel, ball),
            chat_client=chat_client if switchable else None,
            on_model_switched=on_model_switched if switchable else None,
            theme_choice=theme_choice,
            on_theme_change=on_theme_change,
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
                self.assistant.set_status("session" if self.assistant.in_session else "idle")
            except Exception:
                import logging

                logging.getLogger(__name__).exception("文字指令执行失败")

        threading.Thread(target=job, daemon=True).start()
