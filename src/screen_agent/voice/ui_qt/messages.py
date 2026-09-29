"""会话消息列表：Model/View + QTextDocument Delegate（Qt 聊天 UI 生产级模式）。

- MessageModel：消息数据（kind: User/Bot/Info + text）
- MessageDelegate：paint 与 sizeHint 用同一份 QTextDocument 测量——高度天然精确，
  富文本/换行/对齐全部精确，QLabel sizeHint 不可信的问题从架构上消失
- MessageView：QListView 组合（透明背景、像素级滚动、自动滚底、内容高度计算）
"""

from __future__ import annotations

import html
from typing import Any, Callable

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QTextDocument, QTextOption
from PyQt6.QtWidgets import QAbstractItemView, QListView, QStyleOptionViewItem, QStyledItemDelegate

KIND_USER = "User"
KIND_BOT = "Bot"
KIND_INFO = "Info"

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
        return None

    def add_message(self, kind: str, text: str) -> None:
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append({"kind": kind, "text": text})
        self.endInsertRows()

    def set_last_text(self, text: str) -> None:
        """流式/打字机：原地更新最后一条（Bot）。"""
        if not self._items:
            return
        self._items[-1]["text"] = text
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

    def _doc(self, kind: str, text: str, width: float) -> QTextDocument:
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
            doc.setHtml(
                f"<span style='color:{t['marker']};font-size:9px;'>&#9679;</span>&nbsp;&nbsp;"
                f"<span style='font-size:12px;color:{t['text']};'>{_escape(text)}</span>"
            )
        doc.setTextWidth(max(60.0, width))
        return doc

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        kind = index.data(Qt.ItemDataRole.UserRole) or KIND_BOT
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        width = max(80.0, float(option.rect.width()) - _PAD_H * 2) if option.rect.width() > 0 else 260.0
        doc = self._doc(kind, text, width)
        return QSize(int(width), int(doc.size().height()) + _PAD_V * 2)

    def paint(self, painter, option, index) -> None:  # noqa: ANN001
        kind = index.data(Qt.ItemDataRole.UserRole) or KIND_BOT
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        rect = option.rect
        width = max(80.0, float(rect.width()) - _PAD_H * 2)
        doc = self._doc(kind, text, width)
        h = doc.size().height()

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        if kind == KIND_USER:
            # Claude 式右竖线：贴内容右缘
            line_x = rect.right() - _PAD_H
            painter.setPen(QColor(self._t["user_line"]))
            painter.setBrush(QColor(self._t["user_line"]))
            painter.drawRect(
                int(line_x), int(rect.top() + _PAD_V), 2, max(4, int(h) - 4)
            )
        painter.translate(rect.left() + _PAD_H, rect.top() + _PAD_V)
        doc.drawContents(painter)
        painter.restore()


class MessageView(QListView):
    """透明消息列表：组装 model + delegate，附自动滚底与内容高度计算。"""

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

    def add_message(self, kind: str, text: str) -> None:
        self._model.add_message(kind, text)
        self.scrollToBottom()
        if self._on_content_change:
            self._on_content_change()

    def stream_last(self, text: str) -> None:
        self._model.set_last_text(text)
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
