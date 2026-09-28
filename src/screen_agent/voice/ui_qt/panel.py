"""输入交互：Spotlight 式紧凑浮条 + 按需生长的回复卡。

形态借鉴 Raycast / Spotlight / ChatGPT 桌面伴侣窗（紧凑、按需生长、键盘优先），
视觉用自己的语言：深海军蓝玻璃面 + 流光点睛（顶部细条与焦点描边），
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

PANEL_W = 420              # 卡片内容宽度
SHADOW_MARGIN = 14         # 窗口留给手绘投影的边距
INPUT_MIN_H = 44
INPUT_MAX_H = 132          # 约 6 行
REPLY_MAX_H = 260          # 回复卡限高，超出内部滚动
LONG_TEXT_THRESHOLD = 400  # 超过这么多字折叠成 chip
CHIP_TEMPLATE = "[已粘贴长文 {n} 字 · 回车发送]"

STYLE = """
#Panel {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #171c25, stop:1 #11151c);
    border: 1px solid #333c4b;
    border-radius: 16px;
}
#Panel[focused="true"] { border: 1px solid #4c5b73; }
#InputBox {
    background: transparent; color: #f8fafc;
    border: none; padding: 10px 4px 10px 12px; font-size: 14px;
    selection-background-color: #3b82f6;
}
#SendBtn {
    color: #ffffff; background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #2563eb, stop:1 #7c3aed);
    border: none; border-radius: 14px; font-size: 13px;
    min-width: 54px; max-width: 54px; min-height: 30px; max-height: 30px;
}
#SendBtn:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #3b82f6, stop:1 #8b5cf6); }
#CloseBtn {
    color: #6b7686; background: transparent; border: none;
    font-size: 12px; max-width: 20px; max-height: 20px;
}
#CloseBtn:hover { color: #f8fafc; }
#ClearBtn {
    color: #6b7686; background: transparent; border: none; font-size: 11px;
}
#ClearBtn:hover { color: #c084fc; }
#Strip {
    border-radius: 1px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0894FF, stop:0.35 #C959DD, stop:0.7 #FF2E54, stop:1 #FF9004);
    max-height: 2px;
}
#ReplyCard { background: #0d1117; border: 1px solid #232b38; border-radius: 12px; }
#Status { color: #7d8798; font-size: 11px; }
#ModelLabel { color: #6b7686; font-size: 11px; }
#ModelBtn {
    color: #9aa6b8; background: transparent; border: none;
    font-size: 11px; padding: 2px 6px; border-radius: 6px;
}
#ModelBtn:hover { color: #f8fafc; background: #232b38; }
"""

# 气泡样式直接写在控件上：动态加入 QScrollArea 的控件拿不到祖先样式表，
# 依赖级联会出现「用户气泡没底色」这类偶发失效。
BUBBLE_STYLES = {
    "User": (
        "QLabel { color: #f8fafc; font-size: 13px; padding: 8px 11px;"
        " background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2563eb, stop:1 #4f46e5);"
        " border-radius: 11px; border-bottom-right-radius: 4px; margin-left: 56px; }"
    ),
    "Bot": (
        "QLabel { color: #f8fafc; font-size: 13px; padding: 8px 11px;"
        " background: #212936; border: 1px solid #2f3949;"
        " border-radius: 11px; border-bottom-left-radius: 4px; margin-right: 56px; }"
    ),
    "Info": (
        "QLabel { color: #86efac; font-size: 12px; padding: 8px 11px;"
        " background: #14322a; border: 1px solid #1f4a3c;"
        " border-radius: 9px; margin: 0 30px; }"
    ),
}

DOT_COLORS = {
    "idle": "#8b95a8",
    "listening": "#34d399",
    "processing": "#60a5fa",
    "speaking": "#c084fc",
    "session": "#34d399",
}


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

    # ---- 高度自适应 ----

    def _adjust_height(self) -> None:
        doc_h = int(self.document().size().height()) + 22
        target = max(INPUT_MIN_H, min(INPUT_MAX_H, doc_h))
        if target != self.height():
            self.setFixedHeight(target)
            self.height_changed.emit()

    # ---- 长文折叠 ----

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
        """取真正要发送的内容（长文 chip 还原为原文）。"""
        if self._long_text is not None:
            return self._long_text
        return self.toPlainText().strip()

    def clear_all(self) -> None:
        self._long_text = None
        self.clear()
        self.setFixedHeight(INPUT_MIN_H)
        self.height_changed.emit()

    # ---- 键盘 ----

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


def _bubble(text: str, kind: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(f"Bubble{kind}")
    label.setStyleSheet(BUBBLE_STYLES.get(kind, BUBBLE_STYLES["Bot"]))
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
    label.setMaximumWidth(PANEL_W - 120)
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
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self._on_submit_text = on_submit_text
        self._activity_fn = activity_fn
        self._on_height_changed = on_height_changed
        self._model_options = list(model_options or [])
        self._current_model = current_model
        self._on_model_change = on_model_change

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN + 2)

        self._frame = QFrame()
        self._frame.setObjectName("Panel")
        outer.addWidget(self._frame)

        root = QVBoxLayout(self._frame)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        # 顶部流光细条（品牌点睛，克制）
        strip = QLabel()
        strip.setObjectName("Strip")
        strip.setFixedHeight(2)
        root.addWidget(strip)

        # ---- 输入行 ----
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

        # ---- 状态行 ----
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        self._dot = QLabel()
        self._dot.setFixedSize(7, 7)
        self._dot.setStyleSheet("border-radius: 3px; background: #8b95a8;")
        self._status_label = QLabel("待唤醒 · 喊「小光」")
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

        # ---- 回复卡（默认隐藏）----
        self._scroll = QScrollArea()
        self._scroll.setObjectName("ReplyCard")
        self._scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setStyleSheet("background: #0d1117;")
        self._chat_flow = QVBoxLayout(inner)
        self._chat_flow.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chat_flow.setSpacing(6)
        self._chat_flow.setContentsMargins(8, 8, 8, 8)
        self._scroll.setWidget(inner)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(0)
        self._scroll.hide()
        root.addWidget(self._scroll)

        # ---- 最近屏幕活动：极简一行 ----
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

        self._relayout()

    # ---- 手绘投影（真实层次，且不糊文字）----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = self.rect().adjusted(
            SHADOW_MARGIN, SHADOW_MARGIN, -SHADOW_MARGIN, -(SHADOW_MARGIN + 2)
        )
        for i in range(6):
            spread = 6 - i
            alpha = 8 + i * 5
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, alpha))
            path = QPainterPath()
            path.addRoundedRect(
                float(card.left() - spread), float(card.top() - spread + 3),
                float(card.width() + spread * 2), float(card.height() + spread * 2),
                16 + spread, 16 + spread,
            )
            painter.drawPath(path)
        painter.end()

    def _on_input_focus(self, focused: bool) -> None:
        self._frame.setProperty("focused", "true" if focused else "false")
        self._frame.setStyleSheet(
            "#Panel { border: 1px solid %s; }" % ("#4c5b73" if focused else "#333c4b")
        )

    # ---- 高度自适应 ----

    def _relayout(self) -> None:
        """按内容重算高度：输入条 + （有回复时的回复卡）。无内容时只有一行高。"""
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
        self._chat_flow.addWidget(_bubble(text, kind), alignment=Qt.AlignmentFlag.AlignTop)
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
        """点模型名直接切换（不用进托盘菜单）。"""
        from PyQt6.QtGui import QAction, QActionGroup
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #1b212b; color: #f8fafc; border: 1px solid #333c4b;"
            " border-radius: 8px; padding: 4px; }"
            "QMenu::item { padding: 6px 18px; border-radius: 6px; font-size: 12px; }"
            "QMenu::item:selected { background: #2b3646; }"
        )
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
            "idle": "待唤醒 · 喊「小光」",
            "listening": "正在听…",
            "processing": "思考中…",
            "speaking": "播报中…",
            "session": "连续对话中",
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
        """Esc：有内容先清空，空了再收起。"""
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
