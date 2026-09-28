"""展开面板：对话气泡流 + 文字输入 + 最近屏幕活动卡片。"""

from __future__ import annotations

import threading
from typing import Callable

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

PANEL_W, PANEL_H = 320, 440

STYLE = """
#Panel {
    background: rgba(13, 17, 23, 244);
    border: 1px solid rgba(148, 163, 184, 55);
    border-radius: 16px;
}
#Title { color: #f1f5f9; font-size: 14px; font-weight: 600; }
#Status { color: #94a3b8; font-size: 11px; }
#Dot { border-radius: 4px; background: #8b95a8; max-width: 8px; max-height: 8px; }
#BubbleUser, #BubbleBot, #BubbleInfo {
    color: #f1f5f9; font-size: 12px; padding: 8px 11px; line-height: 130%;
}
#BubbleUser {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(59, 130, 246, 200), stop:1 rgba(37, 99, 235, 190));
    border-radius: 12px; border-bottom-right-radius: 4px; margin-left: 44px;
}
#BubbleBot {
    background: rgba(31, 41, 55, 200);
    border-radius: 12px; border-bottom-left-radius: 4px; margin-right: 44px;
}
#BubbleInfo {
    background: rgba(52, 211, 153, 70);
    border-radius: 10px; margin: 0 24px; color: #d1fae5;
}
#InputBox {
    background: rgba(30, 37, 48, 220); color: #f1f5f9;
    border: 1px solid rgba(148, 163, 184, 70); border-radius: 10px;
    padding: 7px 12px; font-size: 12px;
    selection-background-color: #3b82f6;
}
#InputBox:focus { border: 1px solid rgba(96, 165, 250, 160); }
#ActivityCard {
    color: #8b95a5; font-size: 10px; padding: 7px 9px;
    background: rgba(30, 37, 48, 130); border-radius: 9px;
}
"""


def _bubble(text: str, kind: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(f"Bubble{kind}")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setMaximumWidth(PANEL_W - 70)
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

        header = QHBoxLayout()
        dot = QLabel()
        dot.setObjectName("Dot")
        dot.setFixedSize(8, 8)
        self._dot = dot
        title = QLabel("Agent-Retina")
        title.setObjectName("Title")
        self._status_label = QLabel("待唤醒")
        self._status_label.setObjectName("Status")
        header.addWidget(dot)
        header.addSpacing(6)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._status_label)
        root.addLayout(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("background: transparent;")
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self._chat_flow = QVBoxLayout(inner)
        self._chat_flow.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chat_flow.setSpacing(6)
        self._scroll.setWidget(inner)
        root.addWidget(self._scroll, 1)

        self._activity = QLabel("最近活动加载中…")
        self._activity.setObjectName("ActivityCard")
        self._activity.setWordWrap(True)
        root.addWidget(self._activity)

        self._input = QLineEdit()
        self._input.setObjectName("InputBox")
        self._input.setPlaceholderText("打字也行，回车发送")
        self._input.returnPressed.connect(self._submit)
        root.addWidget(self._input)

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
