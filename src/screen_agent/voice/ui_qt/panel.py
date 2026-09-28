"""输入交互：Spotlight 式紧凑浮条 + 按需生长的回复卡（浅色/深色双主题）。

形态借鉴 Raycast / Spotlight / ChatGPT 桌面伴侣窗（紧凑、按需生长、键盘优先），
视觉用自己的语言：柔和玻璃面 + 流光点睛（顶部细条、聚焦描边），
投影手绘（不用 QGraphicsDropShadowEffect——它会把自绘文字一起栅格化发糊）。
"""

from __future__ import annotations

import threading
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QShortcut
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

PANEL_W = 356              # 窗口宽（含投影边距）
SHADOW_MARGIN = 10
INPUT_MIN_H = 40
INPUT_MAX_H = 112          # 约 5 行
REPLY_MAX_H = 196          # 回复卡限高，超出内部滚动
LONG_TEXT_THRESHOLD = 400  # 超过这么多字折叠成 chip
CHIP_TEMPLATE = "[已粘贴长文 {n} 字 · 回车发送]"

THEMES = {
    "dark": {
        "panel_top": "#2a3140",
        "panel_bottom": "#1e242e",
        "panel_border": "#3d4757",
        "panel_border_focus": "#5b6a83",
        "text": "#f5f8fc",
        "sub": "#a3aebf",
        "muted": "#78849a",
        "reply_bg": "#1a212b",
        "reply_border": "#2b3441",
        "bubble_bot_bg": "#2b3442",
        "bubble_bot_border": "#3a455a",
        "bubble_info_bg": "#17372c",
        "bubble_info_border": "#245440",
        "bubble_info_text": "#8ce9b6",
        "btn_hover_bg": "#333d4d",
        "shadow_alpha": 26,
    },
    "light": {
        "panel_top": "#ffffff",
        "panel_bottom": "#f6f8fc",
        "panel_border": "#dde4ef",
        "panel_border_focus": "#9db4d6",
        "text": "#1b2330",
        "sub": "#5c6a7d",
        "muted": "#8d99ab",
        "reply_bg": "#f3f6fb",
        "reply_border": "#e2e8f2",
        "bubble_bot_bg": "#e9eef7",
        "bubble_bot_border": "#d7dfec",
        "bubble_info_bg": "#e7f7ef",
        "bubble_info_border": "#c6e9d9",
        "bubble_info_text": "#116b4b",
        "btn_hover_bg": "#eef2f9",
        "shadow_alpha": 18,
    },
}

AURORA_STRIP = (
    "background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
    " stop:0 #0894FF, stop:0.35 #C959DD, stop:0.7 #FF2E54, stop:1 #FF9004);"
)

DOT_COLORS = {
    "idle": "#8b95a8",
    "listening": "#34d399",
    "processing": "#3b82f6",
    "speaking": "#a855f7",
    "session": "#34d399",
}


def build_style(theme: str) -> str:
    t = THEMES.get(theme, THEMES["light"])
    return f"""
#Panel {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t['panel_top']}, stop:1 {t['panel_bottom']});
    border: 1px solid {t['panel_border']};
    border-radius: 16px;
}}
#InputBox {{
    background: transparent; color: {t['text']};
    border: none; padding: 9px 4px 9px 11px; font-size: 13px;
    selection-background-color: #3b82f6; selection-color: #ffffff;
}}
#SendBtn {{
    color: #ffffff;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2563eb, stop:1 #7c3aed);
    border: none; border-radius: 14px; font-size: 13px;
    min-width: 46px; max-width: 46px; min-height: 27px; max-height: 27px;
}}
#SendBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3b82f6, stop:1 #8b5cf6);
}}
#CloseBtn {{
    color: {t['muted']}; background: transparent; border: none;
    font-size: 12px; max-width: 20px; max-height: 20px;
}}
#CloseBtn:hover {{ color: {t['text']}; }}
#ClearBtn {{ color: {t['muted']}; background: transparent; border: none; font-size: 11px; }}
#ClearBtn:hover {{ color: #7c3aed; }}
#ModelBtn {{
    color: {t['sub']}; background: transparent; border: none;
    font-size: 11px; padding: 2px 6px; border-radius: 6px;
}}
#ModelBtn:hover {{ color: {t['text']}; background: {t['btn_hover_bg']}; }}
#Strip {{ border-radius: 1px; {AURORA_STRIP} max-height: 2px; }}
#ReplyCard {{ background: {t['reply_bg']}; border: 1px solid {t['reply_border']}; border-radius: 12px; }}
#Status {{ color: {t['sub']}; font-size: 11px; }}
"""


def build_bubble_style(kind: str, theme: str) -> str:
    t = THEMES.get(theme, THEMES["light"])
    if kind == "User":
        return (
            "QLabel { color: #ffffff; font-size: 12px; padding: 7px 10px;"
            " background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2563eb, stop:1 #4f46e5);"
            " border-radius: 11px; border-bottom-right-radius: 4px; margin-left: 40px; }"
        )
    if kind == "Info":
        return (
            f"QLabel {{ color: {t['bubble_info_text']}; font-size: 11px; padding: 6px 9px;"
            f" background: {t['bubble_info_bg']}; border: 1px solid {t['bubble_info_border']};"
            " border-radius: 9px; margin: 0 30px; }"
        )
    return (
        f"QLabel {{ color: {t['text']}; font-size: 12px; padding: 7px 10px;"
        f" background: {t['bubble_bot_bg']}; border: 1px solid {t['bubble_bot_border']};"
        " border-radius: 11px; border-bottom-left-radius: 4px; margin-right: 40px; }"
    )


def build_menu_style(theme: str) -> str:
    t = THEMES.get(theme, THEMES["light"])
    return (
        f"QMenu {{ background: {t['panel_top']}; color: {t['text']};"
        f" border: 1px solid {t['panel_border']}; border-radius: 8px; padding: 4px; }}"
        f"QMenu::item {{ padding: 6px 18px; border-radius: 6px; font-size: 12px; }}"
        f"QMenu::item:selected {{ background: {t['btn_hover_bg']}; }}"
    )


class InputEdit(QTextEdit):
    """自增高输入框：Enter 发送、Shift+Enter 换行、长文粘贴折叠成 chip。"""

    submitted = pyqtSignal()
    escape_pressed = pyqtSignal()
    height_changed = pyqtSignal()
    focus_changed = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("InputBox")
        self.setPlaceholderText("打字或说话都行 · Enter 发送")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(INPUT_MIN_H)
        self._long_text: str | None = None
        self.document().contentsChanged.connect(self._adjust_height)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.focus_changed.emit(False)

    def _adjust_height(self) -> None:
        doc_h = int(self.document().size().height()) + 22
        target = max(INPUT_MIN_H, min(INPUT_MAX_H, doc_h))
        if target != self.height():
            self.setFixedHeight(target)
            self.height_changed.emit()

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        text = source.text() or ""
        if len(text) > LONG_TEXT_THRESHOLD:
            self._long_text = text
            self.setPlainText(CHIP_TEMPLATE.format(n=len(text)))
            self._adjust_height()
            return
        super().insertFromMimeData(source)
        self._adjust_height()

    def payload(self) -> str:
        if self._long_text is not None:
            return self._long_text
        return self.toPlainText().strip()

    def clear_all(self) -> None:
        self._long_text = None
        self.clear()
        self.setFixedHeight(INPUT_MIN_H)
        self.height_changed.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.submitted.emit()
            return
        if key == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            return
        super().keyPressEvent(event)


def _bubble(text: str, kind: str, theme: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(f"Bubble{kind}")
    label.setStyleSheet(build_bubble_style(kind, theme))
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
    label.setMaximumWidth(PANEL_W - 96)
    label.ensurePolished()
    return label


class ChatPanel(QWidget):
    """紧凑输入条 + 按需生长的回复卡（无内容时只有一行高）。"""

    def __init__(
        self,
        signals,
        on_submit_text: Callable[[str], None],
        activity_fn: Callable[[], str] | None = None,
        on_height_changed: Callable[[], None] | None = None,
        model_options: list[str] | None = None,
        current_model: str = "",
        on_model_change: Callable[[str], None] | None = None,
        theme: str = "light",
        wake_hint: str = "喊「瑞塔」",
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._theme = theme
        self.setStyleSheet(build_style(theme))
        self._on_submit_text = on_submit_text
        self._activity_fn = activity_fn
        self._on_height_changed = on_height_changed
        self._model_options = list(model_options or [])
        self._current_model = current_model
        self._on_model_change = on_model_change
        self._wake_hint = wake_hint

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN + 2)

        self._frame = QFrame()
        self._frame.setObjectName("Panel")
        outer.addWidget(self._frame)

        root = QVBoxLayout(self._frame)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        strip = QLabel()
        strip.setObjectName("Strip")
        strip.setFixedHeight(2)
        root.addWidget(strip)

        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        self._input = InputEdit()
        self._input.submitted.connect(self._submit)
        self._input.escape_pressed.connect(self._handle_escape)
        self._input.height_changed.connect(self._relayout)
        self._input.focus_changed.connect(self._on_input_focus)
        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("SendBtn")
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.clicked.connect(self._submit)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("CloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        input_row.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(input_row)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        self._dot = QLabel()
        self._dot.setFixedSize(7, 7)
        self._dot.setStyleSheet("border-radius: 3px; background: #8b95a8;")
        self._status_label = QLabel(f"待唤醒 · {wake_hint}")
        self._status_label.setObjectName("Status")
        self._model_btn = QPushButton("")
        self._model_btn.setObjectName("ModelBtn")
        self._model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_btn.clicked.connect(self._open_model_menu)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("ClearBtn")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self.clear_history)
        meta_row.addWidget(self._dot)
        meta_row.addSpacing(2)
        meta_row.addWidget(self._status_label)
        meta_row.addStretch(1)
        meta_row.addWidget(self._model_btn)
        meta_row.addWidget(clear_btn)
        root.addLayout(meta_row)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("ReplyCard")
        self._scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("ReplyInner")
        self._chat_flow = QVBoxLayout(inner)
        self._chat_flow.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chat_flow.setSpacing(6)
        self._chat_flow.setContentsMargins(7, 7, 7, 7)
        self._scroll.setWidget(inner)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(0)
        self._scroll.hide()
        root.addWidget(self._scroll)

        self._activity = QLabel("")
        self._activity.setObjectName("Status")
        self._activity.hide()
        root.addWidget(self._activity)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._handle_escape)

        self._signals = signals
        signals.status.connect(self._on_status)
        signals.transcript.connect(lambda t: self.add_bubble(t, "User"))
        signals.result.connect(lambda t: self.add_bubble(t, "Bot"))

        self._reply_visible = False
        self._activity_visible = False
        self._activity_timer = QTimer(self)
        self._activity_timer.timeout.connect(self._refresh_activity)
        self._activity_timer.start(30000)

        self.set_theme(theme)
        self._relayout()

    # ---- 主题 ----

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self.setStyleSheet(build_style(theme))
        t = THEMES.get(theme, THEMES["light"])
        inner = self._scroll.widget()
        if inner is not None:
            inner.setStyleSheet(f"background: {t['reply_bg']};")
        for i in range(self._chat_flow.count()):
            widget = self._chat_flow.itemAt(i).widget()
            if isinstance(widget, QLabel):
                kind = widget.objectName().replace("Bubble", "") or "Bot"
                widget.setStyleSheet(build_bubble_style(kind, theme))
        self._on_input_focus(self._input.hasFocus())

    # ---- 手绘投影（真实层次，且不糊文字）----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = self.rect().adjusted(
            SHADOW_MARGIN, SHADOW_MARGIN, -SHADOW_MARGIN, -(SHADOW_MARGIN + 2)
        )
        base_alpha = THEMES.get(self._theme, THEMES["light"])["shadow_alpha"]
        for i in range(6):
            spread = 6 - i
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(base_alpha * (0.35 + i * 0.12))))
            path = QPainterPath()
            path.addRoundedRect(
                float(card.left() - spread), float(card.top() - spread + 3),
                float(card.width() + spread * 2), float(card.height() + spread * 2),
                16 + spread, 16 + spread,
            )
            painter.drawPath(path)
        painter.end()

    def _on_input_focus(self, focused: bool) -> None:
        t = THEMES.get(self._theme, THEMES["light"])
        color = t["panel_border_focus"] if focused else t["panel_border"]
        self._frame.setStyleSheet(
            f"#Panel {{ border: 1px solid {color}; border-radius: 16px;"
            f" background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {t['panel_top']}, stop:1 {t['panel_bottom']}); }}"
        )

    # ---- 高度自适应 ----

    def _relayout(self) -> None:
        reply_h = self._scroll.height() if self._reply_visible else 0
        activity_h = self._activity.sizeHint().height() if self._activity_visible else 0
        meta_h = max(16, self._status_label.sizeHint().height())
        total = (
            SHADOW_MARGIN + 10 + 2 + 6 + self._input.height() + 4 + meta_h
            + (6 + reply_h if reply_h else 0)
            + (4 + activity_h if activity_h else 0)
            + 10 + SHADOW_MARGIN + 2
        )
        self.setFixedWidth(PANEL_W)
        self.setFixedHeight(int(total))
        if self._on_height_changed is not None:
            self._on_height_changed()

    def _grow_reply_card(self) -> None:
        content_h = self._chat_flow.sizeHint().height() + 18
        target = max(56, min(REPLY_MAX_H, content_h))
        self._reply_visible = True
        if not self._scroll.isVisible():
            self._scroll.show()
        if target != self._scroll.height():
            self._scroll.setFixedHeight(target)
        self._relayout()

    # ---- 对话流 ----

    def add_bubble(self, text: str, kind: str) -> None:
        if not text:
            return
        self._chat_flow.addWidget(
            _bubble(text, kind, self._theme), alignment=Qt.AlignmentFlag.AlignTop
        )
        bar = self._scroll.verticalScrollBar()
        QTimer.singleShot(30, lambda: bar.setValue(bar.maximum()))
        QTimer.singleShot(0, self._grow_reply_card)

    def add_info(self, text: str) -> None:
        self.add_bubble(text, "Info")

    def set_model_label(self, name: str) -> None:
        self._current_model = name
        if not self._model_options:
            self._model_btn.hide()
            return
        self._model_btn.setText(f"{name} ▾")
        self._model_btn.show()

    def _open_model_menu(self) -> None:
        """点模型名直接切换（不必进托盘菜单）。"""
        from PyQt6.QtGui import QAction, QActionGroup
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.setStyleSheet(build_menu_style(self._theme))
        group = QActionGroup(menu)
        group.setExclusive(True)
        for name in self._model_options:
            act = QAction(name, menu)
            act.setCheckable(True)
            act.setChecked(name == self._current_model)
            act.triggered.connect(lambda _checked, n=name: self._switch_model(n))
            group.addAction(act)
            menu.addAction(act)
        menu.exec(self._model_btn.mapToGlobal(self._model_btn.rect().bottomLeft()))

    def _switch_model(self, name: str) -> None:
        if name == self._current_model:
            return
        self.set_model_label(name)
        if self._on_model_change is not None:
            self._on_model_change(name)

    def focus_input(self) -> None:
        self._input.setFocus()

    def clear_history(self) -> None:
        while self._chat_flow.count():
            item = self._chat_flow.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._scroll.hide()
        self._scroll.setFixedHeight(0)
        self._reply_visible = False
        self._relayout()

    # ---- 状态 ----

    def _on_status(self, status: str) -> None:
        mapping = {
            "idle": f"待唤醒 · {self._wake_hint}",
            "listening": "正在听…",
            "processing": "思考中…",
            "speaking": "播报中…",
            "session": "连续对话中",
        }
        dot_colors = {
            "idle": "#8b95a8",
            "listening": "#34d399",
            "processing": "#60a5fa",
            "speaking": "#c084fc",
            "session": "#34d399",
        }
        self._status_label.setText(mapping.get(status, status))
        color = DOT_COLORS.get(status, "#8b95a8")
        self._dot.setStyleSheet(f"border-radius: 3px; background: {color};")

    def _refresh_activity(self) -> None:
        if self._activity_fn is None:
            return

        def job() -> None:
            try:
                text = self._activity_fn()
            except Exception:
                text = ""
            QTimer.singleShot(0, lambda: self._apply_activity(text))

        threading.Thread(target=job, daemon=True).start()

    def _apply_activity(self, text: str) -> None:
        first = (text or "").split("\n")[0].strip()
        if not first:
            self._activity.hide()
            self._activity_visible = False
        else:
            self._activity.setText(f"最近：{first[:38]}")
            self._activity.show()
            self._activity_visible = True
        self._relayout()

    # ---- 提交 ----

    def _handle_escape(self) -> None:
        if self._input.payload():
            self._input.clear_all()
        else:
            self.hide()

    def _submit(self) -> None:
        text = self._input.payload()
        if not text:
            return
        preview = text if len(text) <= LONG_TEXT_THRESHOLD else text[:200] + f" … （共 {len(text)} 字）"
        self._input.clear_all()
        self.add_bubble(preview, "User")
        self._on_submit_text(text)
