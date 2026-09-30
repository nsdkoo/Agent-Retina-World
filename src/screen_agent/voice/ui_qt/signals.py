"""跨线程信号桥：语音线程 emit，UI 主线程槽。"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal


class AssistantSignals(QObject):
    status = pyqtSignal(str)        # idle / listening / processing / speaking / session
    transcript = pyqtSignal(str)    # 语音识别中间/最终文本
    partial = pyqtSignal(str)       # 流式回话增量（累计全文）
    result = pyqtSignal(str)        # 执行结果 / 助手回复
    session = pyqtSignal(bool)      # 连续对话开/关
    prompt = pyqtSignal(str, list)  # 需要用户拍板：问题 + 候选按钮（点选后回传指令）
    progress = pyqtSignal(str)      # 任务执行进度：Agent 每步一行，小字显示、不播报
    # 主动建议：助手在用户空闲时主动说的一句（「接着做 X 吗」），不是对提问的回答。
    # 单独一个通道而不是混进 result —— 它不该出现在对话流里
    suggestion = pyqtSignal(list)   # list[dict]：每条含 kind / what / why / action
    # 后果预演：动手前的后果（含可逆性与风险）。**单独一条路而不是混进 progress** ——
    # 它是警示，不是进度；混在一起用户会当噪音划过去，那这一层就白做了
    foresight = pyqtSignal(dict)    # {tool, goal, change, reversible, risks, ...}
