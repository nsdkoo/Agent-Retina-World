"""展开面板：对话气泡流 + 文字输入 + 最近屏幕活动卡片。

可读性优先：面板底色全不透明，气泡纯色实底；
不再依赖焦点自动收起（点面板内任何地方都不会消失），Esc 或 × 关闭。
"""

from __future__ import annotations

import threading
from typing import Callable

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

PANEL_W, PANEL_H = 360, 500

STYLE = """
#Panel {
    background: #14181f;
    border: 1px solid #3a4250;
    border-radius: 14px;
}
#Title { color: #f8fafc; font-size: 14px; font-weight: 600; }
#Status, #ModelLabel { color: #9aa6b8; font-size: 11px; }
#CloseBtn {
    color: #9aa6b8; background: #232a36; border: none; border-radius: 6px;
    font-size: 12px; max-width: 22px; max-height: 22px;
}
#CloseBtn:hover { background: #303948; color: #f8fafc; }
#Dot { border-radius: 4px; background: #8b95a8; max-width: 8px; max-height: 8px; }
#Divider {
    border-radius: 1px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0894FF, stop:0.35 #C959DD, stop:0.7 #FF2E54, stop:1 #FF9004);
    max-height: 2px;
}
#BubbleUser, #BubbleBot, #BubbleInfo {
    color: #f8fafc; font-size: 13px; padding: 9px 12px;
}
#BubbleUser {
    background: #2563eb; border-radius: 12px;
    border-bottom-right-radius: 4px; margin-left: 48px;
}
#BubbleBot {
    background: #262e3b; border-radius: 12px;
    border-bottom-left-radius: 4px; margin-right: 48px;
}
#BubbleInfo { background: #1b3a2e; border-radius: 10px; margin: 0 26px; color: #86efac; }
#Scroll { background: #10141b; border: none; border-radius: 10px; }
#ActivityCard {
    color: #9aa6b8; font-size: 11px; padding: 8px 10px;
    background: #1a2029; border-radius: 9px;
}
#InputBox {
    background: #1e2530; color: #f8fafc;
    border: 1px solid #3a4250; border-radius: 10px;
    padding: 9px 12px; font-size: 13px;
    selection-background-color: #3b82f6;
}
#InputBox:focus { border: 1px solid #60a5fa; }
#SendBtn {
    color: #f8fafc; background: #2563eb; border: none; border-radius: 10px;
    font-size: 13px; max-width: 56px;
}
#SendBtn:hover { background: #3b82f6; }
"""


def _bubble(text: str, kind: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(f"Bubble{kind}")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
    label.setMaximumWidth(PANEL_W - 90)
    return label


class ChatPanel(QWidget):
    """悬浮球单击展开的对话面板。"""

    def __init__(
        self,
        signals,
        on_submit_text: Callable[[str], None],
        activity_fn: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(PANEL_W, PANEL_H)
        self.setStyleSheet(STYLE)
        self._on_submit_text = on_submit_text
        self._activity_fn = activity_fn

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # ---- 标题行 ----
        header = QHBoxLayout()
        dot = QLabel()
        dot.setObjectName("Dot")
        dot.setFixedSize(8, 8)
        self._dot = dot
        title = QLabel("Agent-Retina")
        title.setObjectName("Title")
        self._model_label = QLabel()
        self._model_label.setObjectName("ModelLabel")
        self._status_label = QLabel("待唤醒")
        self._status_label.setObjectName("Status")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("CloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)

        header.addWidget(dot)
        header.addSpacing(6)
        header.addWidget(title)
        header.addSpacing(8)
        header.addWidget(self._model_label)
        header.addStretch(1)
        header.addWidget(self._status_label)
        header.addSpacing(8)
        header.addWidget(close_btn)
        root.addLayout(header)

        divider = QLabel()
        divider.setObjectName("Divider")
        divider.setFixedHeight(2)
        root.addWidget(divider)

        # ---- 对话流 ----
        self._scroll = QScrollArea()
        self._scroll.setObjectName("Scroll")
        self._scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setStyleSheet("background: #10141b;")
        self._chat_flow = QVBoxLayout(inner)
        self._chat_flow.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chat_flow.setSpacing(6)
        self._chat_flow.setContentsMargins(8, 8, 8, 8)
        self._scroll.setWidget(inner)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self._scroll, 1)

        # ---- 屏幕活动卡片 ----
        self._activity = QLabel("最近活动加载中…")
        self._activity.setObjectName("ActivityCard")
        self._activity.setWordWrap(True)
        root.addWidget(self._activity)

        # ---- 输入行：输入框 + 发送按钮 ----
        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        self._input = QLineEdit()
        self._input.setObjectName("InputBox")
        self._input.setPlaceholderText("打字或说话都行，回车发送")
        self._input.returnPressed.connect(self._submit)
        self._input.setMinimumHeight(38)
        send_btn = QPushButton("发送")
        send_btn.setObjectName("SendBtn")
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setMinimumHeight(38)
        send_btn.clicked.connect(self._submit)
        input_row.addWidget(self._input, 1)
        input_row.addWidget(send_btn)
        root.addLayout(input_row)

        # Esc 关闭
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.hide)

        self._signals = signals
        signals.status.connect(self._on_status)
        signals.transcript.connect(lambda t: self.add_bubble(t, "User"))
        signals.result.connect(lambda t: self.add_bubble(t, "Bot"))

        self._activity_timer = QTimer(self)
        self._activity_timer.timeout.connect(self._refresh_activity)
        self._activity_timer.start(30000)
        QTimer.singleShot(300, self._refresh_activity)

    # ---- 对话流 ----

    def add_bubble(self, text: str, kind: str) -> None:
        if not text:
            return
        self._chat_flow.addWidget(_bubble(text, kind), alignment=Qt.AlignmentFlag.AlignTop)
        bar = self._scroll.verticalScrollBar()
        QTimer.singleShot(30, lambda: bar.setValue(bar.maximum()))

    def add_info(self, text: str) -> None:
        self.add_bubble(text, "Info")

    def set_model_label(self, name: str) -> None:
        self._model_label.setText(name)

    def _on_status(self, status: str) -> None:
        mapping = {
            "idle": "待唤醒 · 喊「小光」",
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
        self._dot.setStyleSheet(
            f"#Dot {{ border-radius: 4px; background: {dot_colors.get(status, '#8b95a8')}; "
            f"max-width: 8px; max-height: 8px; }}"
        )

    def _refresh_activity(self) -> None:
        if self._activity_fn is None:
            return

        def job() -> None:
            try:
                text = self._activity_fn()
            except Exception:
                text = ""
            QTimer.singleShot(0, lambda: self._activity.setText(text or "最近没有屏幕活动记录"))

        threading.Thread(target=job, daemon=True).start()

    def _submit(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.add_bubble(text, "User")
        self._on_submit_text(text)
