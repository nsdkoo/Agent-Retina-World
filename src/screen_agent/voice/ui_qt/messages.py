"""会话消息列表：Model/View + QTextDocument Delegate（Qt 聊天 UI 生产级模式）。

- MessageModel：消息数据（kind: User/Bot/Info + text）
- MessageDelegate：paint 与 sizeHint 用同一份 QTextDocument 测量——高度天然精确，
  富文本/换行/对齐全部精确，QLabel sizeHint 不可信的问题从架构上消失
- MessageView：QListView 组合（透明背景、像素级滚动、自动滚底、内容高度计算）
"""

from __future__ import annotations

import html
from typing import Any, Callable

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QTextDocument, QTextOption
from PyQt6.QtWidgets import QAbstractItemView, QListView, QStyleOptionViewItem, QStyledItemDelegate

KIND_USER = "User"
KIND_BOT = "Bot"
KIND_INFO = "Info"

REVEAL_ROLE = int(Qt.ItemDataRole.UserRole) + 1  # 流式出字位置（None=非流式）
ACTION_ROLE = int(Qt.ItemDataRole.UserRole) + 2  # 这条消息可点的动作（None=不可点）
_FADE_TAIL = 6                                    # 尾部渐入字符数

_PAD_V = 4
_PAD_H = 2


def _escape(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


class MessageModel(QAbstractListModel):
    """消息数据：[{kind, text}]。"""

    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict[str, str]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item["text"]
        if role == Qt.ItemDataRole.UserRole:
            return item["kind"]
        if role == REVEAL_ROLE:
            return item.get("reveal")
        if role == ACTION_ROLE:
            return item.get("action")
        return None

    def add_message(self, kind: str, text: str, reveal: int | None = None,
                    action: dict | None = None) -> None:
        """加一条消息。

        `action` 不为空表示这条**可以点**（点击后执行该动作）。
        存结构化 dict 而不是拼个字符串——动作最终要按下标参数调真实方法。
        """
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append({"kind": kind, "text": text, "reveal": reveal, "action": action})
        self.endInsertRows()

    def set_last_text(self, text: str, reveal: int | None = None) -> None:
        """流式/打字机：原地更新最后一条（Bot），reveal=出字位置供尾部渐入。"""
        if not self._items:
            return
        self._items[-1]["text"] = text
        self._items[-1]["reveal"] = reveal
        idx = self.index(len(self._items) - 1)
        self.dataChanged.emit(idx, idx)

    def clear(self) -> None:
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()


class MessageDelegate(QStyledItemDelegate):
    """paint 与 sizeHint 共用同一 QTextDocument 测量——高度精确的根源。"""

    def __init__(self, palette: dict) -> None:
        super().__init__()
        self._t = palette

    def set_palette(self, t: dict) -> None:
        self._t = t

    def _doc(self, kind: str, text: str, width: float, reveal_pos: int | None = None) -> QTextDocument:
        t = self._t
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        if kind == KIND_USER:
            doc.setHtml(
                f"<div align='right'><span style='font-size:12px;font-weight:600;"
                f"color:{t['user_text']};'>{_escape(text)}</span></div>"
            )
        elif kind == KIND_INFO:
            doc.setHtml(
                f"<span style='font-size:11px;color:{t['muted']};'>{_escape(text)}</span>"
            )
        else:
            body_html = _escape(text)
            # 流式尾部渐入：最新 6 字颜色从背景色渐变到正文色（Smooth Reveal 的 fade-in tail）
            if reveal_pos is not None and 0 <= reveal_pos < len(text):
                body_html = self._fade_tail_html(text, reveal_pos)
            doc.setHtml(
                f"<span style='color:{t['marker']};font-size:9px;'>&#9679;</span>&nbsp;&nbsp;"
                f"<span style='font-size:12px;color:{t['text']};'>{body_html}</span>"
            )
        doc.setTextWidth(max(60.0, width))
        return doc

    def _fade_tail_html(self, text: str, reveal_pos: int) -> str:
        """尾部渐入：最新字符最接近背景色（最淡），向前逐字加深到正文色。"""
        import html as _html

        t = self._t
        bg = QColor(t.get("reply_bg", t.get("panel_bottom", "#f4f2ea")))
        fg = QColor(t["text"])

        def blend(ratio: float) -> str:
            r = int(bg.red() + (fg.red() - bg.red()) * ratio)
            g = int(bg.green() + (fg.green() - bg.green()) * ratio)
            b = int(bg.blue() + (fg.blue() - bg.blue()) * ratio)
            return f"#{r:02x}{g:02x}{b:02x}"

        tail_start = max(0, reveal_pos - _FADE_TAIL)
        head = _escape(text[:tail_start])
        tail_html = ""
        for offset in range(reveal_pos - tail_start):
            ch = text[tail_start + offset]
            # offset=0 是渐入区最老的字（最实），最新字最淡
            ratio = 0.12 + 0.88 * ((offset + 1) / _FADE_TAIL)
            tail_html += f"<span style='color:{blend(min(1.0, ratio))};'>{_escape(ch)}</span>"
        return head + tail_html

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        kind = index.data(Qt.ItemDataRole.UserRole) or KIND_BOT
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        reveal = index.data(REVEAL_ROLE)
        width = max(80.0, float(option.rect.width()) - _PAD_H * 2) if option.rect.width() > 0 else 260.0
        if kind == KIND_USER:
            width = max(80.0, width - 72)  # 右侧内缩块：竖线 + 文字
        doc = self._doc(kind, text, width, reveal if isinstance(reveal, int) else None)
        return QSize(int(option.rect.width() if option.rect.width() > 0 else width), int(doc.size().height()) + _PAD_V * 2)

    def paint(self, painter, option, index) -> None:  # noqa: ANN001
        kind = index.data(Qt.ItemDataRole.UserRole) or KIND_BOT
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        reveal = index.data(REVEAL_ROLE)
        reveal_pos = reveal if isinstance(reveal, int) else None
        rect = option.rect
        width = max(80.0, float(rect.width()) - _PAD_H * 2)
        line_x = None
        if kind == KIND_USER:
            # 右侧内缩块：文字靠右 + 金棕竖线，与助手消息明确区分
            width = max(80.0, width - 72)
            line_x = rect.right() - 10
        doc = self._doc(kind, text, width, reveal_pos)
        h = doc.size().height()

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        if line_x is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self._t["user_line"]))
            painter.drawRect(
                int(line_x), int(rect.top() + _PAD_V + 1), 2, max(4, int(h) - 2)
            )
        painter.translate(rect.left() + _PAD_H, rect.top() + _PAD_V)
        doc.drawContents(painter)
        painter.restore()


class MessageView(QListView):
    """透明消息列表：组装 model + delegate，附自动滚底与内容高度计算。

    `action_clicked` 会在用户点了**带动作的那条消息**时发出。
    自绘列表本来不处理点击（delegate 只管画），所以要自己在 `mousePressEvent`
    里定位命中了哪一行 —— 这是「面板里的建议也能点」的落点。
    """

    action_clicked = pyqtSignal(dict)

    def __init__(self, palette: dict, on_content_change: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._palette = palette
        self._on_content_change = on_content_change
        self._model = MessageModel()
        self._delegate = MessageDelegate(palette)
        self.setModel(self._model)
        self.setItemDelegate(self._delegate)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSpacing(6)
        self.setStyleSheet(
            "QListView { background: transparent; border: none; }"
            "QListView::item { border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(120,132,150,120); border-radius: 3px; min-height: 24px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }"
        )

    # ---- 对外接口 ----

    def set_palette(self, t: dict) -> None:
        self._palette = t
        self._delegate.set_palette(t)
        self._model.layoutChanged.emit()

    def add_message(self, kind: str, text: str, reveal: int | None = None,
                    action: dict | None = None) -> None:
        self._model.add_message(kind, text, reveal, action)
        self.scrollToBottom()
        if self._on_content_change:
            self._on_content_change()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """点消息列表：命中带动作的那条就发信号。

        **只处理左键**；右键要留给列表自己的行为（选择、右键菜单）。
        delegate 是自绘的，命中判断只能靠 `indexAt` 拿行号，
        再回头问 model 这一行有没有 action —— 没有就照常交给父类。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                action = index.data(ACTION_ROLE)
                if action:
                    self.action_clicked.emit(dict(action))
                    return                      # 命中就别再冒泡，避免误触发选择
        super().mousePressEvent(event)

    def stream_last(self, text: str, reveal: int | None = None) -> None:
        self._model.set_last_text(text, reveal)
        self.scrollToBottom()
        if self._on_content_change:
            self._on_content_change()

    def clear_messages(self) -> None:
        self._model.clear()
        if self._on_content_change:
            self._on_content_change()

    def has_messages(self) -> bool:
        return self._model.rowCount() > 0

    def content_height(self) -> int:
        """全部消息的展示高度之和（含间距），供外层卡片生长计算。"""
        n = self._model.rowCount()
        if n == 0:
            return 0
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, self.viewport().width(), 100)
        total = 0
        for row in range(n):
            idx = self._model.index(row)
            sh = self.itemDelegate().sizeHint(opt, idx)
            total += sh.height()
        return int(total + self.spacing() * (n - 1))
