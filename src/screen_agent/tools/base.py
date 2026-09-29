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


class ConfirmationNeeded(RuntimeError):
    """HIGH 级工具需要用户确认时抛出，registry 捕获后原样透传 message。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
