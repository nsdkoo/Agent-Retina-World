"""ToolRegistry：注册制工具分发 + 危险分级 + 确认钩子（纯逻辑，无平台依赖）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from screen_agent.tools.base import ActionResult, ConfirmationNeeded


class PlatformError(RuntimeError):
    """非 Windows 平台 / 底层 API 不可用。"""


class RiskLevel(IntEnum):
    SAFE = 0    # 只读：列表/查询/读剪贴板
    LOW = 1     # 可逆常规：打开应用/网页/设剪贴板
    HIGH = 2    # 需确认：鼠标点击/键入/关应用/锁屏


# 类型转换失败的哨兵。用独立对象而不是 None——
# None 可能是合法的转换结果（参数值本身就是 None），混用会把"转换成功但值为 None"误判成失败
_FAILED = object()

# 只做白名单内的安全转换。JSON 只给 LLM 传字符串的场景很常见（"3" / "true"），
# 顺手纠正是有价值的；但别做任意解析——猜错了比直接报错更难查
_COERCE_BOOL_TRUE = ("true", "1", "yes", "是")
_COERCE_BOOL_FALSE = ("false", "0", "no", "否")


def _coerce(value, want: str):  # noqa: ANN001, ANN202
    """把字符串转成期望的基础类型，转不了返回 `_FAILED`。"""
    if not isinstance(value, str):
        return _FAILED
    text = value.strip()
    if want == "integer":
        body = text[1:] if text.startswith("-") else text
        return int(text) if body.isdigit() else _FAILED
    if want == "number":
        try:
            return float(text)
        except ValueError:
            return _FAILED
    if want == "boolean":
        low = text.lower()
        if low in _COERCE_BOOL_TRUE:
            return True
        if low in _COERCE_BOOL_FALSE:
            return False
        return _FAILED
    return _FAILED


def _classify_error(exc: Exception) -> str:
    """把异常归到重试策略能用的一类上。

    分类依据是**下一步该怎么办**，不是异常本身叫什么：
    临时的（网络、占用）可以重试；文件找不到说明这条路走不通、该换条路；
    其余的一律不重试——宁可少救，不可重试出副作用。
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "retryable"
    if isinstance(exc, OSError):
        text = str(exc)
        if any(word in text for word in (
            "找不到", "不存在", "已存在", "不是目录", "拒绝访问",
            "not found", "already exists", "No such file",
        )):
            return "correctable"
        return "retryable"
    return "fatal"


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., ActionResult]
    risk: RiskLevel = RiskLevel.LOW
    params_doc: dict[str, str] = field(default_factory=dict)
    # 可校验的参数 schema：{"path": {"type": "string", "required": True}}
    # **不填完全按现状工作**（退回用 params_doc 推断），所以是增量落地，不用一次改完所有工具
    schema: dict[str, dict] | None = None
    # 同一个调用重复执行是否安全。**默认 False（保守）**：
    # move 重跑会再移一次、mkdir 重跑会报已存在——只有纯读工具才敢标 True
    idempotent: bool = False


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

    def list_openai_tools(self, max_risk: RiskLevel | None = None) -> list[dict]:
        """ToolSpec → OpenAI function calling schema（喂给 LLM 自主选工具）。

        有 `schema` 时用它生成**正确的 JSON 类型与 required**，从源头减少模型传错类型；
        没有 schema 的工具退回 `params_doc` 推断（全当 string 且全部必填，与旧行为一致）。
        """
        tools = []
        for spec in self.list_tools(max_risk=max_risk):
            if spec.schema:
                props = {
                    key: {
                        "type": rule.get("type", "string"),
                        "description": rule.get("description", ""),
                    }
                    for key, rule in spec.schema.items()
                }
                required = [k for k, rule in spec.schema.items() if rule.get("required")]
            else:
                props = {k: {"type": "string", "description": v} for k, v in spec.params_doc.items()}
                required = list(props.keys())
            tools.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                },
            })
        return tools

    _TYPE_MAP = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
    }

    def validate(self, spec: ToolSpec, params: dict) -> tuple[bool, str, dict]:
        """按 schema 校验参数，返回 (是否通过, 错误说明, 纠正后的参数)。

        只做**白名单内的安全转换**（"3"→3、"true"→True），不做任意解析——
        猜错了比直接报错更难查。
        没有 schema 的工具直接放行，所以这是增量落地，不用一次改完所有工具。
        """
        if not spec.schema:
            return True, "", dict(params)

        cleaned: dict = {}
        for key in params:
            if key not in spec.schema:
                return False, f"未知参数 {key}", {}

        for key, rule in spec.schema.items():
            want = rule.get("type", "string")
            if key not in params:
                if rule.get("required"):
                    return False, f"缺少必填参数 {key}", {}
                continue
            value = params[key]
            expected = self._TYPE_MAP.get(want)
            if expected is None or isinstance(value, expected):
                cleaned[key] = value
                continue
            coerced = _coerce(value, want)
            if coerced is _FAILED:
                return False, f"参数 {key} 类型不对（期望 {want}）", {}
            cleaned[key] = coerced
        return True, "", cleaned

    def run(self, name: str, **params) -> ActionResult:
        spec = self._tools.get(name)
        if spec is None:
            return ActionResult(success=False, message=f"未知工具：{name}", error_kind="fatal")

        # 参数校验前置：类型错/缺必填在这里就被拦下，不再掉进下面的 except Exception
        # 被兜成含糊的「执行失败」——否则错误恢复机制分不清「参数写错了」和「网络抖了」
        ok, why, cleaned = self.validate(spec, params)
        if not ok:
            return ActionResult(
                success=False, message=f"参数有误：{why}", error_kind="invalid_params"
            )
        params = cleaned

        if spec.risk >= RiskLevel.HIGH and self._confirm_fn is not None:
            try:
                if not self._confirm_fn(spec, params):
                    return ActionResult(success=False, message=f"已取消：{spec.description}")
            except ConfirmationNeeded as exc:
                return ActionResult(success=False, message=exc.message)
            except Exception as exc:  # noqa: BLE001 - 确认钩子失败视为取消
                return ActionResult(success=False, message=f"确认失败已取消：{exc}")
        try:
            return spec.handler(**params)
        except PlatformError as exc:
            return ActionResult(
                success=False, message=f"此功能仅支持 Windows：{exc}", error_kind="fatal"
            )
        except Exception as exc:  # noqa: BLE001 - 统一兜底，与旧 executor.run 语义一致
            return ActionResult(
                success=False,
                message=f"执行失败：{exc}",
                error_kind=_classify_error(exc),
            )
