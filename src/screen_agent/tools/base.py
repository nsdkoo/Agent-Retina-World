"""工具层基础类型：ActionResult（打破 tools→voice 反向依赖的正式位置）。

voice.executor 保留同名 re-export 兼容旧引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActionResult:
    success: bool
    message: str
    detail: dict | None = field(default_factory=dict)
    # 需要用户拍板时给出的候选（UI 渲染成可点按钮，点选后原样作为下一条指令提交）
    options: list[str] | None = None
    # 失败分类，给错误恢复用。空串表示成功或未分类。
    #   retryable        瞬时错误（网络抖动、临时占用）——可重试
    #   correctable      参数/前置条件问题（找不到文件、已存在）——可重规划
    #   fatal            环境或权限问题（非 Windows、无权限）——不该重试
    #   timeout_unknown  超时且无法确定副作用是否已发生——**不能当作失败重试**
    #   invalid_params   入参校验没过——在 handler 之前就拦下了
    error_kind: str = ""


class ConfirmationNeeded(RuntimeError):
    """HIGH 级工具需要用户确认时抛出，registry 捕获后原样透传 message。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
