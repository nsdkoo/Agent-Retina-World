"""跨线程信号桥：语音线程 emit，UI 主线程槽。"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal


class AssistantSignals(QObject):
    status = pyqtSignal(str)        # idle / listening / processing / speaking / session
    transcript = pyqtSignal(str)    # 语音识别中间/最终文本
    partial = pyqtSignal(str)       # 流式回话增量（累计全文）
    result = pyqtSignal(str)        # 执行结果 / 助手回复
    session = pyqtSignal(bool)      # 连续对话开/关
