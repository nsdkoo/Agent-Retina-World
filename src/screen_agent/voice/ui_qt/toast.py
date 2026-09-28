"""会话气泡：AI 回话时自动浮现在状态点旁边，不用点开面板。

- 半透明磨砂气泡（浅/深主题各一套），带指向状态点的小尖角，像在跟你说话
- 自动出现 + 自动消失：播报结束再等几秒收起；聆听中的实时转写边听边显示
- 不抢焦点（WA_ShowWithoutActivating + WindowDoesNotAcceptFocus），打字不被打断
- 点气泡可展开完整面板
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QPropertyAnimation, QEasingCurve, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget

MAX_W = 292
MIN_W = 84
PAD_X, PAD_Y = 11, 8
RADIUS = 12
TAIL = 6
FADE_MS = 160

THEMES = {
    "light": {
        "bg": QColor(253, 252, 247, 247),
        "border": QColor(230, 225, 208, 220),
        "text": QColor("#292620"),
        "bot_bg": QColor(248, 246, 239, 248),
        "user_bg": QColor(201, 100, 66, 242),
        "user_text": QColor("#ffffff"),
        "info_bg": QColor(233, 239, 224, 246),
        "info_text": QColor("#4f7040"),
        "shadow": 7,
    },
    "dark": {
        "bg": QColor(36, 32, 25, 240),
        "border": QColor(58, 53, 43, 200),
        "text": QColor("#f2eee4"),
        "bot_bg": QColor(42, 37, 29, 242),
        "user_bg": QColor(59, 130, 246, 235),
        "user_text": QColor("#ffffff"),
        "info_bg": QColor(37, 48, 31, 240),
        "info_text": QColor("#a9c79b"),
        "shadow": 12,
    },
}


class BubbleToast(QWidget):
    """自动出现/消失的对话气泡。"""

    def __init__(self, theme: str = "light", on_click: Callable[[], None] | None = None) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._theme = theme
        self._on_click = on_click
        self._text = ""
        self._kind = "Bot"          # Bot | User | Info
        self._tail_on_left = True   # 尖角朝左（气泡在点左边时）
        self._anchor = QPointF(0, 0)

        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

        self.hide()

    # ---- 主题 ----

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self._relayout()
        self.update()

    # ---- 内容 ----

    def show_message(
        self,
        text: str,
        kind: str = "Bot",
        timeout_ms: int | None = None,
        anchor: QPointF | None = None,
    ) -> None:
        """显示一条消息；timeout_ms 为 None 表示常驻（如聆听中的实时转写）。"""
        text = (text or "").strip()
        if not text:
            return
        self._text = text
        self._kind = kind
        if anchor is not None:
            self._anchor = anchor
        self._hide_timer.stop()
        self._relayout()
        self._place()
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
        self._fade(1.0)
        if timeout_ms is not None:
            self._hide_timer.start(timeout_ms)

    def hide_now(self) -> None:
        self._hide_timer.stop()
        self._fade_out()

    def set_timeout(self, timeout_ms: int) -> None:
        """给当前气泡设置自动收起时间（播报结束后调用）。"""
        if self.isVisible():
            self._hide_timer.start(timeout_ms)

    # ---- 尺寸与定位 ----

    def _metrics(self):
        from PyQt6.QtGui import QFont, QFontMetrics

        font = QFont()
        font.setPixelSize(12)
        return QFontMetrics(font)

    def _relayout(self) -> None:
        fm = self._metrics()
        max_text_w = MAX_W - PAD_X * 2 - TAIL
        rect = fm.boundingRect(0, 0, max_text_w, 2000, int(Qt.TextFlag.TextWordWrap), self._text)
        text_w = max(MIN_W - PAD_X * 2, min(max_text_w, rect.width()))
        text_h = rect.height()
        self._text_rect = QRectF(PAD_X, PAD_Y, text_w, text_h)
        self.setFixedSize(int(text_w + PAD_X * 2 + TAIL), int(text_h + PAD_Y * 2))

    def set_anchor(self, anchor: QPointF) -> None:
        self._anchor = anchor
        if self.isVisible():
            self._place()

    def _place(self) -> None:
        """贴在状态点左侧（放不下就右侧），垂直居中于点。"""
        from PyQt6.QtWidgets import QApplication

        screen = QApplication.screenAt(self._anchor.toPoint()) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        x = int(self._anchor.x() - self.width() - 6)
        self._tail_on_left = False
        if x < avail.left() + 4:
            x = int(self._anchor.x() + 6)
            self._tail_on_left = True
        y = int(self._anchor.y() - self.height() / 2)
        y = max(avail.top() + 4, min(y, avail.bottom() - self.height() - 4))
        self.move(x, y)

    # ---- 绘制 ----

    def paintEvent(self, event) -> None:  # noqa: N802
        t = THEMES.get(self._theme, THEMES["light"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        body = self.rect().adjusted(0, 0, -TAIL, 0) if not self._tail_on_left else self.rect().adjusted(TAIL, 0, 0, 0)
        body_rect = QRectF(body)

        # 柔和投影
        for i in range(4):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(t["shadow"] * (0.3 + i * 0.18))))
            spread = 4 - i
            painter.drawRoundedRect(
                body_rect.adjusted(-spread, -spread + 2, spread, spread + 2),
                RADIUS + spread, RADIUS + spread,
            )

        # 气泡 + 尖角
        path = QPainterPath()
        path.addRoundedRect(body_rect, RADIUS, RADIUS)
        cy = body_rect.center().y()
        if self._tail_on_left:
            tail = QPolygonF([
                QPointF(body_rect.left() + 1, cy - TAIL),
                QPointF(body_rect.left() - TAIL + 1, cy),
                QPointF(body_rect.left() + 1, cy + TAIL),
            ])
        else:
            tail = QPolygonF([
                QPointF(body_rect.right() - 1, cy - TAIL),
                QPointF(body_rect.right() + TAIL - 1, cy),
                QPointF(body_rect.right() - 1, cy + TAIL),
            ])
        path.addPolygon(tail)
        path = path.simplified()

        if self._kind == "User":
            fill, border, text_color = t["user_bg"], t["user_bg"], t["user_text"]
        elif self._kind == "Info":
            fill, border, text_color = t["info_bg"], t["info_bg"], t["info_text"]
        else:
            fill, border, text_color = t["bot_bg"], t["border"], t["text"]

        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawPath(path)

        # 顶部发丝线（与面板一致的中性分隔）
        painter.setPen(Qt.PenStyle.NoPen)
        line_color = THEMES.get(self._theme, THEMES["light"])["border"]
        line_rect = QRectF(body_rect.left() + 12, body_rect.top() + 1.2, body_rect.width() - 24, 1.0)
        painter.setBrush(line_color)
        painter.drawRect(line_rect)

        # 文本
        from PyQt6.QtGui import QFont

        font = QFont()
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(text_color)
        text_rect = self._text_rect.translated(TAIL if self._tail_on_left else 0, 0)
        painter.drawText(text_rect, int(Qt.TextFlag.TextWordWrap), self._text)
        painter.end()

    # ---- 淡入淡出 ----

    def _fade(self, to: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(to)
        self._anim.start()

    def _fade_out(self) -> None:
        if not self.isVisible():
            return
        self._fade(0.0)
        QTimer.singleShot(FADE_MS + 20, self.hide)

    # ---- 交互：点气泡展开完整面板 ----

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._on_click:
            self._on_click()
