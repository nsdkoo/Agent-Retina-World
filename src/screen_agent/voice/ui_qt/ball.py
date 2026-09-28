"""悬浮球窗口：视网膜主题玻璃球 + 状态动画 + 音量呼吸环 + 拖动 + 单击展开面板。"""

from __future__ import annotations

import math
import sys
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import QGraphicsDropShadowEffect, QWidget

from screen_agent.voice.ui_qt.signals import AssistantSignals

BALL_SIZE = 76

# 状态主色（光环/瞳孔）
STATE_COLORS = {
    "idle": QColor("#8b95a8"),
    "listening": QColor("#34d399"),
    "processing": QColor("#60a5fa"),
    "speaking": QColor("#c084fc"),
    "session": QColor("#34d399"),
}


class BallWidget(QWidget):
    """悬浮球本体（无边框、置顶、半透明、可拖动、可点击）。"""

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
        self._phase = 0.0
        self._level = 0.0  # 平滑后的音量 0-1
        self._drag_offset = None
        self._press_pos = None
        self._moved = False

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 130))
        self.setGraphicsEffect(shadow)

        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.right() - BALL_SIZE - 28, geo.top() + geo.height() // 3)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        self._signals.status.connect(self.set_status)

    def set_status(self, status: str) -> None:
        if status in STATE_COLORS:
            self._status = status

    def _tick(self) -> None:
        self._phase = (self._phase + 0.055) % (2 * math.pi)
        if self._level_fn is not None:
            try:
                raw = max(0.0, min(1.0, float(self._level_fn())))
            except Exception:
                raw = 0.0
            self._level = raw * 0.55 + self._level * 0.45
        self.update()

    # ---- 绘制 ----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(10, 10, BALL_SIZE - 20, BALL_SIZE - 20)
        cx, cy = rect.center().x(), rect.center().y()
        r = rect.width() / 2
        accent = QColor(STATE_COLORS.get(self._status, STATE_COLORS["idle"]))
        t = time.time()

        # 1. 状态光环（最外层）
        self._draw_halo(painter, cx, cy, r, accent, t)

        # 2. 玻璃球体
        grad = QRadialGradient(cx - r * 0.35, cy - r * 0.45, r * 2.1)
        grad.setColorAt(0.0, QColor("#43506b"))
        grad.setColorAt(0.45, QColor("#232c3d"))
        grad.setColorAt(1.0, QColor("#0b0f16"))
        painter.setPen(QPen(QColor(255, 255, 255, 34), 1.4))
        painter.setBrush(grad)
        painter.drawEllipse(rect)

        # 3. 虹膜（retina 主题）
        iris_r = r * 0.60
        iris_grad = QRadialGradient(cx, cy, iris_r)
        dim = QColor(accent)
        dim.setAlpha(200 if self._status != "idle" else 120)
        iris_grad.setColorAt(0.0, dim)
        iris_grad.setColorAt(0.72, QColor(15, 20, 30))
        iris_grad.setColorAt(1.0, QColor("#0b0f16"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(iris_grad)
        painter.drawEllipse(QPointF(cx, cy), iris_r, iris_r)

        # 4. 瞳孔 + 高光
        pupil_r = iris_r * (0.34 + 0.10 * self._level if self._status == "listening" else 0.36)
        painter.setBrush(QColor("#05070b"))
        painter.drawEllipse(QPointF(cx, cy), pupil_r, pupil_r)
        glow = QColor(accent)
        glow.setAlpha(170)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(cx, cy), pupil_r * 0.38, pupil_r * 0.38)

        # 5. 玻璃高光弧
        path = QPainterPath()
        path.arcMoveTo(rect.adjusted(4, 4, -4, -4), 200)
        path.arcTo(rect.adjusted(4, 4, -4, -4), 200, 80)
        painter.setPen(QPen(QColor(255, 255, 255, 70), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        painter.end()

    def _draw_halo(self, painter: QPainter, cx: float, cy: float, r: float, accent: QColor, t: float) -> None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._status == "listening":
            # 音量呼吸环：随实际声音起伏
            breathe = 1.5 + 1.2 * math.sin(self._phase * 2)
            halo_r = r + 3 + breathe + self._level * 7.0
            color = QColor(accent)
            color.setAlpha(150)
            painter.setPen(QPen(color, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawEllipse(QPointF(cx, cy), halo_r, halo_r)
            # 第二圈残影
            color2 = QColor(accent)
            color2.setAlpha(max(0, 70 - int(self._level * 60)))
            painter.setPen(QPen(color2, 1.6))
            painter.drawEllipse(QPointF(cx, cy), halo_r + 4 + self._level * 5, halo_r + 4 + self._level * 5)
        elif self._status == "processing":
            # 旋转弧
            start = int((t * 300) % 360) * 16
            pen = QPen(accent, 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            halo = QRectF(cx - r - 4, cy - r - 4, (r + 4) * 2, (r + 4) * 2)
            painter.drawArc(halo, start, 110 * 16)
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 50), 2.8))
            painter.drawArc(halo, start + 130 * 16, 200 * 16)
        elif self._status == "speaking":
            # 播报波纹
            for i in range(2):
                phase = (self._phase + i * math.pi) % (2 * math.pi)
                rr = r + 2 + phase / (2 * math.pi) * 14
                color = QColor(accent)
                color.setAlpha(max(0, 110 - int(phase / (2 * math.pi) * 110)))
                painter.setPen(QPen(color, 2.0))
                painter.drawEllipse(QPointF(cx, cy), rr, rr)
        else:
            # idle：缓慢呼吸微光
            glow = QColor(accent)
            glow.setAlpha(26 + int(16 * (0.5 + 0.5 * math.sin(self._phase))))
            painter.setPen(QPen(glow, 2.4))
            halo_r = r + 2 + 1.0 * math.sin(self._phase)
            painter.drawEllipse(QPointF(cx, cy), halo_r, halo_r)

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


class QtFloatingBall:
    """总装：悬浮球 + 对话面板 + 托盘 + 语音助手后台线程。"""

    def __init__(self, assistant) -> None:
        self.assistant = assistant

    def _recent_activity(self) -> str:
        try:
            md = self.assistant.pipeline.proactive.timeline_markdown(limit=3)
            lines = [ln.strip() for ln in md.split("\n") if ln.startswith("- ")]
            return "\n".join(lines[:3])
        except Exception:
            return ""

    def run(self) -> None:
        import threading

        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication

        from screen_agent.voice.ui_qt.panel import ChatPanel
        from screen_agent.voice.ui_qt.tray import TrayIcon

        app = QApplication.instance() or QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)

        signals = AssistantSignals()

        level_fn = getattr(self.assistant.audio_loop, "get_level", None)
        panel = ChatPanel(signals, on_submit_text=self._submit_text, activity_fn=self._recent_activity)
        ball = BallWidget(signals, on_click=lambda: self._toggle_panel(panel, ball), level_fn=level_fn)

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
            self.assistant.stop()
            QTimer.singleShot(300, app.quit)

        tray = TrayIcon(ball, on_exit=on_exit, on_toggle_panel=lambda: self._toggle_panel(panel, ball))
        tray.show()
        ball.show()

        # 失焦自动收起面板
        def _on_focus_changed(_old, new) -> None:
            if not panel.isVisible():
                return
            if new is None or (new is not panel and new is not panel._input and not panel.isAncestorOf(new)):
                panel.hide()

        app.focusChanged.connect(_on_focus_changed)

        self.assistant.run_in_background()
        app.exec()

    def _toggle_panel(self, panel, ball) -> None:
        if panel.isVisible():
            panel.hide()
            return
        geo = ball.geometry()
        screen = ball.screen() or panel.screen()
        x = geo.left() - panel.width() - 10
        if screen is not None and x < screen.availableGeometry().left():
            x = geo.right() + 10
        y = geo.top() - 20
        panel.move(x, y)
        panel.show()
        panel.raise_()
        panel._input.setFocus()

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
