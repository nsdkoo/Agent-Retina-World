"""悬浮球窗口：Apple Intelligence 边缘流光 + 深色玻璃体 + 音量呼吸 + 拖动/点击。

设计参考 Siri edge glow（社区逐帧取样配色）：
蓝 #0894FF → 紫 #C959DD → 珊瑚 #FF2E54 → 琥珀 #FF9004 → 循环，锥形渐变旋转。
三层环（wash/bloom/core）叠加出光晕深度，全部手绘，不用 QGraphicsEffect（会栅格化发糊）。
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
    QFont,
    QConicalGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget

from screen_agent.voice.ui_qt.signals import AssistantSignals

logger = logging.getLogger(__name__)

BALL_SIZE = 84

# Apple Intelligence 流光配色（固定顺序，勿打乱）
AURORA = ["#0894FF", "#C959DD", "#FF2E54", "#FF9004"]

# 状态 → (转速 deg/s, 流光强度 0-1, 呼吸幅度)
STATE_TUNING = {
    "idle": (40, 0.40, 0.15),
    "listening": (130, 1.00, 0.10),
    "processing": (220, 0.85, 0.05),
    "speaking": (130, 0.95, 0.20),
    "session": (130, 1.00, 0.10),
}


def _aurora_gradient(cx: float, cy: float, angle: float) -> QConicalGradient:
    grad = QConicalGradient(cx, cy, angle)
    for i, hex_color in enumerate(AURORA):
        grad.setColorAt(i / len(AURORA), QColor(hex_color))
    grad.setColorAt(1.0, QColor(AURORA[0]))
    return grad


class BallWidget(QWidget):
    """悬浮球本体（无边框、置顶、可拖动、可点击、hover 反馈）。"""

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
        self._hover = 0.0  # 0-1 悬停插值
        self._breathe_phase = 0.0
        self._drag_offset = None
        self._press_pos = None
        self._moved = False

        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.right() - BALL_SIZE - 28, geo.top() + geo.height() // 3)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        self._signals.status.connect(self.set_status)

    def set_status(self, status: str) -> None:
        if status in STATE_TUNING:
            self._status = status

    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last_tick)
        self._last_tick = now
        speed, intensity, breathe = STATE_TUNING.get(self._status, STATE_TUNING["idle"])
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

    # ---- 绘制 ----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        speed, intensity, _breathe = STATE_TUNING.get(self._status, STATE_TUNING["idle"])
        level = self._level if self._status == "listening" else 0.0
        hover = self._hover

        cx = cy = BALL_SIZE / 2
        ring_base = 4.0 + level * 2.0 + self._breathe_phase * 2.0
        body_r = BALL_SIZE / 2 - 14 - hover * 1.5  # hover 微放大

        # ---- 1. 三层流光环（wash / bloom / core）----
        layers = [
            # (pen_width, alpha, 半径外扩)
            (9.0 + level * 3.0, 0.10 + intensity * 0.10, 5.0),
            (5.5, 0.22 + intensity * 0.25, 3.0),
            (2.6, 0.75 + intensity * 0.25, 1.5),
        ]
        for width, alpha, offset in layers:
            painter.setOpacity(min(1.0, alpha))
            painter.setPen(QPen(_aurora_gradient(cx, cy, self._angle), width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            rr = body_r + offset
            painter.drawEllipse(QPointF(cx, cy), rr, rr)

        # ---- 2. speaking 外圈涟漪 ----
        if self._status == "speaking":
            for i in range(2):
                phase = (time.monotonic() * 0.9 + i * 0.5) % 1.0
                painter.setOpacity((1.0 - phase) * 0.35)
                painter.setPen(QPen(QColor(AURORA[1]), 1.8))
                rr = body_r + 6 + phase * 16
                painter.drawEllipse(QPointF(cx, cy), rr, rr)

        # ---- 3. 深色玻璃球体 ----
        painter.setOpacity(1.0)
        body_grad = QRadialGradient(cx - body_r * 0.3, cy - body_r * 0.4, body_r * 2.0)
        body_grad.setColorAt(0.0, QColor("#2a3346"))
        body_grad.setColorAt(0.55, QColor("#141a26"))
        body_grad.setColorAt(1.0, QColor("#0a0e15"))
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
        painter.setBrush(body_grad)
        painter.drawEllipse(QPointF(cx, cy), body_r, body_r)

        # 流光在球体边缘的内透光（顶部弧，随强度）
        painter.setOpacity(0.25 + intensity * 0.2)
        inner = QRectF(cx - body_r + 2, cy - body_r + 2, (body_r - 2) * 2, (body_r - 2) * 2)
        painter.setPen(QPen(_aurora_gradient(cx, cy, -self._angle), 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(inner)

        # ---- 4. 顶部高光弧 + hover 提亮 ----
        painter.setOpacity(0.55 + hover * 0.3)
        path = QPainterPath()
        arc_rect = QRectF(cx - body_r + 4, cy - body_r + 4, (body_r - 4) * 2, (body_r - 4) * 2)
        path.arcMoveTo(arc_rect, 205)
        path.arcTo(arc_rect, 205, 75)
        painter.setPen(QPen(QColor(255, 255, 255, 90), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

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

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            was_drag = self._moved
            self._drag_offset = None
            self._press_pos = None
            if not was_drag and self._on_click:
                self._on_click()


def _ui_state_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "ui_state.json"


def load_preferred_model() -> str | None:
    try:
        data = json.loads(_ui_state_path().read_text(encoding="utf-8"))
        return str(data.get("chat_backend") or "") or None
    except Exception:
        return None


def save_preferred_model(name: str) -> None:
    path = _ui_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["chat_backend"] = name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("保存模型偏好失败", exc_info=True)


class QtFloatingBall:
    """总装：悬浮球 + 输入条 + 托盘 + 全局热键 + 语音助手后台线程。"""

    FADE_MS = 150

    def __init__(self, assistant) -> None:
        self.assistant = assistant
        self._panel = None
        self._panel_anim = None
        self._hotkey = None

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

        def on_model_change(name: str) -> None:
            if switchable:
                chat_client.set_preferred(name)
                save_preferred_model(name)
            msg = f"对话模型已切换 → {name}"
            if self._panel is not None:
                self._panel.add_info(msg)
            self.assistant.speak(msg)

        panel = ChatPanel(
            signals,
            on_submit_text=self._submit_text,
            activity_fn=self._recent_activity,
            on_height_changed=self._reposition_panel,
            model_options=chat_client.backend_names if switchable else None,
            current_model=chat_client.current_name if switchable else "",
            on_model_change=on_model_change,
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

        def play_start_chain() -> None:
            self.assistant.audio_loop.set_muted_mic()
            signals.status.emit("speaking")

        def play_end_chain() -> None:
            self.assistant.audio_loop.set_unmuted_mic()
            signals.status.emit("session" if self.assistant.in_session else "idle")

        self.assistant.speaker.on_play_start = play_start_chain
        self.assistant.speaker.on_play_end = play_end_chain

        # 语音线程 → UI 线程
        self.assistant.on_status(signals.status.emit)
        self.assistant.on_transcript(signals.transcript.emit)
        self.assistant.on_result(signals.result.emit)
        self.assistant.on_session(signals.session.emit)

        def on_exit() -> None:
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
        """把浮条贴着球放：优先球左侧，空间不够换右侧；高度变化时保持锚定。"""
        ball, panel = self._ball, self._panel
        geo = ball.geometry()
        screen = ball.screen() or panel.screen()
        avail = screen.availableGeometry() if screen is not None else None

        x = geo.left() - panel.width() + 16
        if avail is not None and x < avail.left() + 8:
            x = geo.right() - 16
        y = geo.top() - 6
        if avail is not None:
            y = min(y, avail.bottom() - panel.height() - 8)
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
