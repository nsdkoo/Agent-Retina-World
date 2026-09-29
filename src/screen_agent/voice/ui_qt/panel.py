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

from screen_agent.voice.ui_qt.messages import MessageView

PANEL_W = 356              # 窗口宽（含投影边距）
SHADOW_MARGIN = 10
INPUT_MIN_H = 72
INPUT_MAX_H = 140          # 约 6 行，备忘录式
REPLY_MAX_H = 420          # 回复区限高（桌面聊天窗惯例：约六成视口高）
LONG_TEXT_THRESHOLD = 400  # 超过这么多字折叠成 chip
CHIP_TEMPLATE = "[已粘贴长文 {n} 字 · 回车发送]"

THEMES = {
    "dark": {
        "panel_top": "#232019",
        "panel_bottom": "#1b1812",
        "panel_border": "#3a352a",
        "panel_border_focus": "#6b5f43",
        "text": "#f2eee3",
        "sub": "#a89f8c",
        "muted": "#7d7463",
        "reply_bg": "#211d15",
        "reply_border": "#383226",
        "bubble_bot_bg": "#2a251c",
        "bubble_bot_border": "#3d362a",
        "bubble_info_bg": "#25301f",
        "bubble_info_border": "#3a4a30",
        "bubble_info_text": "#a9c79b",
        "user_bg": "#3a3020",
        "user_text": "#f5efe0",
        "user_line": "#b45309",
        "marker": "#d97706",
        "accent": "#d97706",
        "btn_hover_bg": "#35302a",
        "shadow_alpha": 12,
    },
    "light": {
        "panel_top": "#fafaf7",
        "panel_bottom": "#f4f2ea",
        "panel_border": "#e4e1d6",
        "panel_border_focus": "#c7bf9e",
        "text": "#2b2721",
        "sub": "#6f695a",
        "muted": "#948c78",
        "reply_bg": "#f1efe5",
        "reply_border": "#e3e0d3",
        "bubble_bot_bg": "#efede2",
        "bubble_bot_border": "#dcd8c8",
        "bubble_info_bg": "#e9efe0",
        "bubble_info_border": "#d2dcc6",
        "bubble_info_text": "#4f7040",
        "user_bg": "#efeadb",
        "user_text": "#2b2721",
        "user_line": "#b45309",
        "marker": "#b45309",
        "accent": "#b45309",
        "btn_hover_bg": "#eeebdd",
        "shadow_alpha": 8,
    },
}

DOT_COLORS = {
    "idle": "#9a917f",
    "listening": "#6aa87f",
    "processing": "#c96442",
    "speaking": "#d97757",
    "session": "#6aa87f",
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
    border: none; padding: 14px 4px 14px 12px; font-size: 13px;
    selection-background-color: #3b82f6; selection-color: #ffffff;
}}
#SendBtn {{
    color: #ffffff; background: #b45309;
    border: none; border-radius: 14px; font-size: 12px;
    min-width: 44px; max-width: 44px; min-height: 26px; max-height: 26px;
}}
#SendBtn:hover {{ background: #c86a0d; }}
#CloseBtn {{
    color: {t['muted']}; background: transparent; border: none;
    font-size: 12px; max-width: 20px; max-height: 20px;
}}
#CloseBtn:hover {{ color: {t['text']}; }}
#ClearBtn {{ color: {t['muted']}; background: transparent; border: none; font-size: 11px; }}
#ClearBtn:hover {{ color: {t['accent']}; }}
#ModelBtn {{
    color: {t['accent']}; background: transparent; border: none;
    font-size: 11px; padding: 2px 6px; border-radius: 6px;
}}
#ModelBtn:hover {{ color: {t['text']}; background: {t['btn_hover_bg']}; }}
#Strip {{ background: {t['panel_border']}; border-radius: 1px; max-height: 1px; }}
#ReplyCard {{ background: transparent; border: none; }}
#ReplyDivider {{ background: {t['reply_border']}; max-height: 1px; }}
#ReplyCard QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px 0; }}
#ReplyCard QScrollBar::handle:vertical {{ background: {t['muted']}; border-radius: 3px; min-height: 24px; }}
#ReplyCard QScrollBar::add-line, #ReplyCard QScrollBar::sub-line {{ height: 0; }}
#ReplyCard QScrollBar::add-page, #ReplyCard QScrollBar::sub-page {{ background: transparent; }}
#Status {{ color: {t['sub']}; font-size: 11px; }}
"""


def build_bubble_style(kind: str, theme: str) -> str:
    """Claude 式排版：用户消息 = 加粗文字 + 左侧 1px 竖线（无底色）；助手 = 纯文本 + 陶土色圆点。"""
    t = THEMES.get(theme, THEMES["light"])
    if kind == "User":
        return (
            f"QLabel {{ color: {t['user_text']}; font-size: 12px; font-weight: 600;"
            f" padding: 2px 9px 2px 0; background: transparent; text-align: right;"
            f" border-right: 2px solid {t['user_line']}; margin-right: 48px; }}"
        )
    if kind == "Info":
        return (
            f"QLabel {{ color: {t['muted']}; font-size: 11px; padding: 2px 0;"
            " background: transparent; margin: 0 4px; }"
        )
    return (
        f"QLabel {{ color: {t['text']}; font-size: 12px; padding: 3px 0;"
        " background: transparent; margin-right: 12px; }"
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


def name_or_hint(wake_hint: str) -> str:
    """从「喊「瑞塔」」这类提示里取名字；取不到就回退通用称呼。"""
    try:
        return wake_hint.split("「")[1].split("」")[0]
    except Exception:
        return "你的桌面助手"


def _bubble(text: str, kind: str, theme: str) -> QLabel:
    if kind == "Bot":
        import html

        safe = html.escape(text).replace(chr(10), "<br>")
        marker = THEMES.get(theme, THEMES["light"])["marker"]
        text = f"<span style='color:{marker}'>&#9679;</span>&nbsp;&nbsp;{safe}"
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.RichText)
    else:
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

        # 空状态欢迎语（有对话后隐藏，清空后重现）
        t0 = THEMES.get(theme, THEMES["light"])
        self._greeting = QWidget()
        self._greeting.setStyleSheet("background: transparent;")
        gl = QVBoxLayout(self._greeting)
        gl.setContentsMargins(0, 16, 0, 20)
        gl.setSpacing(6)
        greet_title = QLabel(f"你好，我是{name_or_hint(wake_hint)}")
        greet_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        greet_title.setStyleSheet(
            f"color: {t0['text']}; font-size: 15px; font-weight: 600; background: transparent;"
        )
        greet_sub = QLabel("有什么可以帮你？打字或语音都可以")
        greet_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        greet_sub.setStyleSheet(
            f"color: {t0['muted']}; font-size: 12px; background: transparent;"
        )
        gl.addWidget(greet_title)
        gl.addWidget(greet_sub)
        root.addWidget(self._greeting)

        # 回复区：透明无盒子，上方一条极细分隔线
        self._reply_divider = QLabel()
        self._reply_divider.setObjectName("ReplyDivider")
        self._reply_divider.setFixedHeight(1)
        self._reply_divider.hide()
        root.addWidget(self._reply_divider)

        self._scroll = MessageView(THEMES.get(theme, THEMES["light"]), on_content_change=self._grow_reply_card)
        self._scroll.setObjectName("ReplyCard")
        self._scroll.setFixedHeight(0)
        self._scroll.hide()
        root.addWidget(self._scroll)

        self._activity = QLabel("")
        self._activity.setObjectName("Status")
        self._activity.hide()
        root.addWidget(self._activity)

        # 顶部：极小的关闭键（常驻，ghost）；状态行容器有对话时才出现
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("CloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "#CloseBtn { color: "
            + THEMES.get(theme, THEMES["light"])["muted"]
            + "; background: transparent; border: none; font-size: 11px;"
            " max-width: 18px; max-height: 18px; }"
            "#CloseBtn:hover { color: "
            + THEMES.get(theme, THEMES["light"])["sub"]
            + "; }"
        )
        close_btn.clicked.connect(self.hide)
        header_row.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(header_row)

        self._meta_container = QWidget()
        meta_row = QHBoxLayout(self._meta_container)
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(6)
        self._status_label = QLabel(f"待唤醒 · {wake_hint}")
        self._status_label.setObjectName("Status")
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("ClearBtn")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self.clear_history)
        meta_row.addWidget(self._status_label)
        meta_row.addStretch(1)
        meta_row.addWidget(clear_btn)
        self._meta_container.hide()
        root.addWidget(self._meta_container)

        # 输入区上方极细分隔线（对话区与输入区分界）
        self._input_divider = QLabel()
        self._input_divider.setObjectName("ReplyDivider")
        self._input_divider.setFixedHeight(1)
        root.addWidget(self._input_divider)

        # 状态点（输入框左侧的呼吸指示）
        self._dot = QLabel()
        self._dot.setFixedSize(7, 7)
        self._dot.setStyleSheet("border-radius: 3px; background: #9a917f;")

        input_row = QHBoxLayout()
        input_row.setSpacing(7)
        # 状态点对齐占位文字第一行（输入区上 padding 14 + 行高一半 ≈ 22）
        dot_wrap = QVBoxLayout()
        dot_wrap.setContentsMargins(0, 19, 0, 0)  # 与占位文字首行垂直居中
        dot_wrap.addWidget(self._dot)
        dot_wrap.addStretch(1)
        input_row.addLayout(dot_wrap)
        self._input = InputEdit()
        self._input.submitted.connect(self._submit)
        self._input.escape_pressed.connect(self._handle_escape)
        self._input.height_changed.connect(self._relayout)
        self._input.focus_changed.connect(self._on_input_focus)
        self._model_btn = QPushButton("")
        self._model_btn.setObjectName("ModelBtn")
        self._model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_btn.clicked.connect(self._open_model_menu)
        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("SendBtn")
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.clicked.connect(self._submit)
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._model_btn, 0, Qt.AlignmentFlag.AlignBottom)
        input_row.addWidget(self._send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(input_row)


        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._handle_escape)

        self._signals = signals
        signals.status.connect(self._on_status)
        signals.transcript.connect(lambda t: self.add_bubble(t, "User"))
        signals.partial.connect(self._on_partial)
        signals.result.connect(self._on_result_text)

        self._reply_visible = False
        self._activity_visible = False
        self._streaming = False
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
        for child in self._greeting.findChildren(QLabel):
            if child.text().startswith("你好"):
                child.setStyleSheet(f"color: {t['text']}; font-size: 15px; font-weight: 600; background: transparent;")
            else:
                child.setStyleSheet(f"color: {t['muted']}; font-size: 12px; background: transparent;")
        self._scroll.set_palette(t)
        self._on_input_focus(self._input.hasFocus())

    # ---- 手绘投影（真实层次，且不糊文字）----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = self.rect().adjusted(
            SHADOW_MARGIN, SHADOW_MARGIN, -SHADOW_MARGIN, -(SHADOW_MARGIN + 2)
        )
        base_alpha = THEMES.get(self._theme, THEMES["light"])["shadow_alpha"]
        for i in range(3):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(60, 48, 28, int(base_alpha * (0.4 + i * 0.25))))
            spread = 3 - i
            path = QPainterPath()
            path.addRoundedRect(
                float(card.left() - spread), float(card.top() - spread + 1.5),
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
        """高度直接取布局 sizeHint——子件高度都已显式锁定，布局自己算总高，不再手拼算术。"""
        hint = self.layout().sizeHint()
        self.setFixedWidth(PANEL_W)
        self.setFixedHeight(int(hint.height()) + 2)
        if self._on_height_changed is not None:
            self._on_height_changed()

    def _grow_reply_card(self) -> None:
        content_h = self._scroll.content_height() + 10
        target = max(44, min(REPLY_MAX_H, content_h))
        self._reply_visible = True
        if not self._scroll.isVisible():
            self._scroll.show()
            self._reply_divider.show()
            self._meta_container.show()
        if target != self._scroll.height():
            self._scroll.setFixedHeight(target)
        self._relayout()

    # ---- 对话流 ----

    def add_bubble(self, text: str, kind: str) -> None:
        if not text:
            return
        self._greeting.hide()
        self._scroll.add_message(kind, text)
        QTimer.singleShot(0, self._grow_reply_card)

    # ---- 流式回话：增量上屏，最后终结；非流式走打字机兜底 ----

    def _on_partial(self, text: str) -> None:
        self._greeting.hide()
        if not self._scroll.has_messages() or self._scroll._model.data(
            self._scroll._model.index(self._scroll._model.rowCount() - 1),
            Qt.ItemDataRole.UserRole,
        ) != "Bot":
            # 首个增量：占位「正在输入…」，随后原地生长
            self._scroll.add_message("Bot", "正在输入…")
        self._streaming = True
        if not text:
            return
        self._scroll.stream_last(text)
        self._grow_reply_card()

    def _on_result_text(self, text: str) -> None:
        if not text:
            return
        if self._streaming and self._scroll.has_messages():
            # 流式路径：原地终结
            self._scroll.stream_last(text)
            self._streaming = False
            self._grow_reply_card()
            return
        self._streaming = False
        self._typewriter_bubble(text)

    def _typewriter_bubble(self, text: str) -> None:
        """非流式兜底：打字机效果逐字显现（模型驱动，delegate 负责样式）。"""
        self._scroll.add_message("Bot", "")
        self._grow_reply_card()
        state = {"i": 0}
        step = max(3, len(text) // 140)

        def tick() -> None:
            state["i"] += step
            i = min(state["i"], len(text))
            self._scroll.stream_last(text[:i])
            if i >= len(text):
                timer.stop()
                timer.deleteLater()
                self._scroll.stream_last(text)

        timer = QTimer(self)
        timer.timeout.connect(tick)
        timer.start(16)

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
        self._streaming = False
        self._scroll.clear_messages()
        self._scroll.hide()
        self._scroll.setFixedHeight(0)
        self._reply_divider.hide()
        self._meta_container.hide()
        self._reply_visible = False
        self._greeting.show()
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
