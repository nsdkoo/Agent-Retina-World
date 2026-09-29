"""消息渲染重构：QLabel → 只读 QTextEdit（富文本高度精确 + Bot 占满行宽）。v2 修正分隔匹配。"""
import pathlib

p = pathlib.Path('src/screen_agent/voice/ui_qt/panel.py')
s = p.read_text(encoding='utf-8')

# 0) 导入 QTextEdit / QFrame
old_imp = s[s.index('from PyQt6.QtWidgets import ('):]
end_imp = old_imp.index(')')
imp_block = old_imp[: end_imp + 1]
if 'QTextEdit' not in imp_block:
    new_imp = imp_block.replace(')', '    QTextEdit,\n    QFrame,\n)')
    s = s.replace(imp_block, new_imp)

# 1) 宽度常量
s = s.replace(
    'PANEL_W = 356              # 窗口宽（含投影边距）',
    'PANEL_W = 356              # 窗口宽（含投影边距）\n'
    'BOT_VIEW_W = PANEL_W - 100 # 助手消息横向占满内容区\n'
    'USER_VIEW_W = PANEL_W - 140 # 用户消息右侧内缩'
)

# 2) build_bubble_style → 声明式（作用于单个 QTextEdit）
old = s[s.index('def build_bubble_style'):s.index('def build_menu_style')]
new = '''def build_bubble_style(kind: str, theme: str) -> str:
    """声明式样式（作用于单个消息视图）：用户 = 加粗 + 右竖线；助手 = 纯文本。"""
    t = THEMES.get(theme, THEMES["light"])
    if kind == "User":
        return (
            f"color: {t['user_text']}; font-size: 12px; font-weight: 600;"
            f" padding: 2px 9px 2px 0; background: transparent;"
            f" border-right: 2px solid {t['user_line']};"
        )
    if kind == "Info":
        return (
            f"color: {t['muted']}; font-size: 11px; padding: 2px 0;"
            "background: transparent;"
        )
    return (
        f"color: {t['text']}; font-size: 12px; padding: 3px 0;"
        "background: transparent;"
    )


'''
s = s.replace(old, new)

# 3) _make_view / _fit_view 替换 _fit_bubble_height
old_fit = '''    def _fit_bubble_height(self, label: QLabel, plain_text: str, pad: int = 12) -> None:
        """QLabel 富文本换行高度不自适应（经典坑）：按内容显式计算并锁定高度。"""
        from PyQt6.QtGui import QFontMetrics

        label.ensurePolished()
        fm = QFontMetrics(label.font())
        w = label.width() or label.maximumWidth()
        br = fm.boundingRect(0, 0, max(60, int(w) - 10), 10000, int(Qt.TextFlag.TextWordWrap), plain_text)
        label.setFixedHeight(br.height() + pad)
'''
new_fit = '''    def _make_view(self, kind: str) -> QTextEdit:
        from PyQt6.QtWidgets import QFrame

        view = QTextEdit()
        view.setObjectName(f"Bubble{kind}")
        view.setReadOnly(True)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setDocumentMargin(0)
        view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        view.setStyleSheet(build_bubble_style(kind, self._theme))
        view.setFixedWidth(USER_VIEW_W if kind == "User" else BOT_VIEW_W)
        return view

    def _fit_view(self, view: QTextEdit) -> None:
        """富文本高度取 QTextDocument 精确布局值（QLabel sizeHint 不可信）。"""
        doc = view.document()
        doc.setTextWidth(max(60, view.width() - 4))
        view.setFixedHeight(int(doc.size().height()) + 6)
'''
assert old_fit in s
s = s.replace(old_fit, new_fit)

# 4) add_bubble
old = '''        align = (
            Qt.AlignmentFlag.AlignRight if kind == "User" else Qt.AlignmentFlag.AlignLeft
        )
        label = _bubble(text, kind, self._theme)
        self._chat_flow.addWidget(
            label, alignment=align | Qt.AlignmentFlag.AlignTop
        )
        pad = 8 if kind == "User" else (4 if kind == "Info" else 12)
        QTimer.singleShot(0, lambda: self._fit_bubble_height(label, text, pad))'''
new = '''        align = (
            Qt.AlignmentFlag.AlignRight if kind == "User" else Qt.AlignmentFlag.AlignLeft
        )
        view = self._make_view(kind)
        if kind == "User":
            import html as _html

            view.setHtml(
                f"<div align='right'>{_html.escape(text).replace(chr(10), '<br>')}</div>"
            )
        else:
            view.setHtml(self._bot_rich_text(text) if kind == "Bot" else text)
        self._chat_flow.addWidget(
            view, alignment=align | Qt.AlignmentFlag.AlignTop
        )
        QTimer.singleShot(0, lambda: self._fit_view(view))'''
assert old in s
s = s.replace(old, new)

# 5) _on_partial
old = '''    def _on_partial(self, text: str) -> None:
        self._greeting.hide()
        if self._pending_reply is None:
            # 占位气泡：正常气泡样式（不斜体），先显示「正在输入…」
            label = QLabel("")
            label.setObjectName("BubbleBot")
            label.setStyleSheet(build_bubble_style("Bot", self._theme))
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
            label.setMaximumWidth(PANEL_W - 96)
            self._chat_flow.addWidget(
                label, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            self._pending_reply = label
            self._grow_reply_card()
            muted = THEMES.get(self._theme, THEMES["light"])["muted"]
            label.setText(f"<span style='color:{muted}'>正在输入…</span>")
        if not text:
            return
        self._pending_reply.setText(self._bot_rich_text(text))
        self._fit_bubble_height(self._pending_reply, text)
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())'''
new = '''    def _on_partial(self, text: str) -> None:
        self._greeting.hide()
        if self._pending_reply is None:
            self._pending_reply = self._make_view("Bot")
            muted = THEMES.get(self._theme, THEMES["light"])["muted"]
            self._pending_reply.setHtml(f"<span style='color:{muted}'>正在输入…</span>")
            self._chat_flow.addWidget(
                self._pending_reply,
                alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            )
            self._grow_reply_card()
        if not text:
            return
        self._pending_reply.setHtml(self._bot_rich_text(text))
        self._fit_view(self._pending_reply)
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())'''
assert old in s
s = s.replace(old, new)

# 6) _on_result_text
old = '''        if self._pending_reply is not None:
            # 流式路径：终结占位气泡
            self._pending_reply.setTextFormat(Qt.TextFormat.RichText)
            self._pending_reply.setText(self._bot_rich_text(text))
            self._fit_bubble_height(self._pending_reply, text)
            self._pending_reply = None'''
new = '''        if self._pending_reply is not None:
            # 流式路径：终结占位气泡
            self._pending_reply.setHtml(self._bot_rich_text(text))
            self._fit_view(self._pending_reply)
            self._pending_reply = None'''
assert old in s
s = s.replace(old, new)

# 7) _typewriter_bubble
old = s[s.index('    def _typewriter_bubble'):s.index('    def add_info')]
new = '''    def _typewriter_bubble(self, text: str) -> None:
        """非流式兜底：打字机效果逐字显现，最后补回圆点标记。"""
        import html

        safe = html.escape(text).replace(chr(10), "<br>")
        full_html = self._bot_rich_text(text)
        view = self._make_view("Bot")
        self._chat_flow.addWidget(
            view, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._grow_reply_card()
        bar = self._scroll.verticalScrollBar()
        state = {"i": 0}
        step = max(3, len(safe) // 140)

        def tick() -> None:
            state["i"] += step
            i = min(state["i"], len(safe))
            view.setPlainText(safe[:i])
            self._fit_view(view)
            bar.setValue(bar.maximum())
            if i >= len(safe):
                timer.stop()
                timer.deleteLater()
                view.setHtml(full_html)
                self._fit_view(view)

        timer = QTimer(self)
        timer.timeout.connect(tick)
        timer.start(16)

'''
s = s.replace(old, new)

# 8) set_theme 气泡重刷：QLabel → QTextEdit
old = '''        for i in range(self._chat_flow.count()):
            widget = self._chat_flow.itemAt(i).widget()
            if isinstance(widget, QLabel):
                kind = widget.objectName().replace("Bubble", "") or "Bot"
                widget.setStyleSheet(build_bubble_style(kind, theme))'''
new = '''        for view in self._scroll.findChildren(QTextEdit):
            kind = view.objectName().replace("Bubble", "") or "Bot"
            view.setStyleSheet(build_bubble_style(kind, theme))'''
assert old in s
s = s.replace(old, new)

# 9) _pending_reply 类型注解
s = s.replace('self._pending_reply: QLabel | None = None', 'self._pending_reply: QTextEdit | None = None')

# 10) 删除已废弃的 _bubble 函数（下一个 def 可能紧邻一个空行）
if 'def _bubble(' in s:
    start = s.index('def _bubble(')
    nxt = s.find('\ndef ', start)
    nxt2 = s.find('\n\ndef ', start)
    ends = [x for x in (nxt, nxt2) if x != -1]
    end = min(ends)
    s = s[:start] + s[end + 1:]

p.write_text(s, encoding='utf-8')
print('view refactor done v2')
