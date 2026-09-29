"""ToolRegistry：注册制工具分发 + 危险分级 + 确认钩子（纯逻辑，无平台依赖）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from screen_agent.voice.executor import ActionResult


class PlatformError(RuntimeError):
    """非 Windows 平台 / 底层 API 不可用。"""


class RiskLevel(IntEnum):
    SAFE = 0    # 只读：列表/查询/读剪贴板
    LOW = 1     # 可逆常规：打开应用/网页/设剪贴板
    HIGH = 2    # 需确认：鼠标点击/键入/关应用/锁屏


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., ActionResult]
    risk: RiskLevel = RiskLevel.LOW
    params_doc: dict[str, str] = field(default_factory=dict)


class ToolRegistry:
    def __init__(
        self,
        confirm_fn: Callable[[ToolSpec, dict], bool] | None = None,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._confirm_fn = confirm_fn

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_tools(self, max_risk: RiskLevel | None = None) -> list[ToolSpec]:
        specs = sorted(self._tools.values(), key=lambda s: s.name)
        if max_risk is None:
            return specs
        return [s for s in specs if s.risk <= max_risk]

    def run(self, name: str, **params) -> ActionResult:
        spec = self._tools.get(name)
        if spec is None:
            return ActionResult(success=False, message=f"未知工具：{name}")
        if spec.risk >= RiskLevel.HIGH and self._confirm_fn is not None:
            try:
                if not self._confirm_fn(spec, params):
                    return ActionResult(success=False, message=f"已取消：{spec.description}")
            except Exception as exc:  # noqa: BLE001 - 确认钩子失败视为取消
                return ActionResult(success=False, message=f"确认失败已取消：{exc}")
        try:
            return spec.handler(**params)
        except PlatformError as exc:
            return ActionResult(success=False, message=f"此功能仅支持 Windows：{exc}")
        except Exception as exc:  # noqa: BLE001 - 统一兜底，与旧 executor.run 语义一致
            return ActionResult(success=False, message=f"执行失败：{exc}")
