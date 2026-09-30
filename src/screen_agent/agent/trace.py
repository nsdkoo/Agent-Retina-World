"""结构化 trace：把一次任务落成可回放、可导出的 span 树。

对标 OpenTelemetry 的 GenAI semantic conventions，但**不引入它的 SDK**——
这是个单机桌面助手，为一个自研 harness 拉一整套 collector/exporter 是过度工程。
所以这里自己定义 span 结构、字段名照着 OTel 的约定起，
将来真想接 Collector 时只差一个 exporter。

几个照抄 OTel 的关键约定：

- `gen_ai.operation.name` 区分 span 类型（`invoke_agent` / `execute_tool` / `guardrail`）
- **`gen_ai.request.model` 与 `gen_ai.response.model` 都要记**：
  服务端可能做了 fallback，实际服务的模型和请求的不一定是同一个
- **内容进 `events` 而不是 `attributes`**：attributes 会被全量索引，
  prompt/参数这类东西塞进去等于把 PII 摊在监控里；events 可以整体关掉

span 层级直接映射到现有的 `task_id + step`，不额外发明概念：

    task (invoke_agent)
    ├── guardrail:policy        权限判定
    ├── function:files.move     工具调用（内容在 events 里）
    └── guardrail:policy
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# span 类型。沿用 OTel/OpenInference 的叫法，别自己造词——
# 将来要导出去时对齐成本最低
KIND_TASK = "task"
KIND_AGENT = "agent"
KIND_FUNCTION = "function"
KIND_GUARDRAIL = "guardrail"
KIND_GENERATION = "generation"

# 敏感内容识别复用隐私闸门那套，避免两处各写一份正则
_SENSITIVE_HINT = ("key", "token", "password", "secret", "卡号", "密码", "身份证")


@dataclass
class Span:
    """一段有始有终的执行。字段名对齐 OTLP 的 span 结构。"""

    name: str
    kind: str
    trace_id: str = ""
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str | None = None
    start_ts: float = field(default_factory=time.time)
    end_ts: float | None = None
    status: str = "ok"                       # ok | error | cancelled
    attributes: dict[str, Any] = field(default_factory=dict)   # 低基数、无 PII
    events: list[dict] = field(default_factory=list)           # 承载内容

    @property
    def duration_ms(self) -> int:
        if self.end_ts is None:
            return 0
        return int((self.end_ts - self.start_ts) * 1000)

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "status": self.status,
            "attributes": self.attributes,
            "events": self.events,
        }


class SpanRecorder:
    """收 span。`content=False` 时**只留结构化元数据、丢掉内容**。

    这是隐私上的关键开关：排障通常只需要知道「第 3 步调了 files.move、花了 120ms、失败了」，
    不需要知道移的是哪个具体文件。
    """

    def __init__(self, content: bool = True) -> None:
        self.content = content

    def start(
        self,
        name: str,
        kind: str,
        *,
        trace_id: str = "",
        parent: Span | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        return Span(
            name=name,
            kind=kind,
            trace_id=trace_id or (parent.trace_id if parent else ""),
            parent_span_id=parent.span_id if parent else None,
            attributes=dict(attributes or {}),
        )

    def finish(self, span: Span, status: str = "ok") -> Span:
        span.end_ts = time.time()
        span.status = status
        return span

    def event(self, span: Span, name: str, attributes: dict[str, Any]) -> None:
        """往 span 上挂一条内容事件。`content=False` 时直接丢弃。"""
        if not self.content:
            return
        span.events.append({"name": name, "ts": time.time(),
                            "attributes": self.redact(attributes)})

    def tool_span(
        self, tool: str, params: dict, *, trace_id: str, parent: Span | None = None,
        step_index: int = 0, risk: str = "",
    ) -> Span:
        """开一个工具调用 span。参数进 events，不进 attributes。"""
        span = self.start(
            f"step:{tool}", KIND_FUNCTION, trace_id=trace_id, parent=parent,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool,
                "tool.risk": risk,
                "step.index": step_index,
            },
        )
        self.event(span, "input", {"tool.parameters": params})
        return span

    def guardrail_span(
        self, decision: str, reason: str, *, trace_id: str, parent: Span | None = None,
        mode: str = "", step_index: int = 0,
    ) -> Span:
        span = self.start(
            "guardrail:policy", KIND_GUARDRAIL, trace_id=trace_id, parent=parent,
            attributes={
                "gen_ai.operation.name": "guardrail",
                "policy.decision": decision,
                "policy.mode": mode,
                "step.index": step_index,
            },
        )
        self.event(span, "decision", {"reason": reason})
        return span

    def agent_span(self, goal: str, *, trace_id: str) -> Span:
        """任务级 span。一次任务 = 一条 trace，trace_id 就是 task_id。"""
        return self.start(
            "task", KIND_TASK, trace_id=trace_id,
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "screen-agent",
                "agent.goal.length": len(goal or ""),
            },
        )

    def redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把看起来敏感的值打码。

        按 key 名判断（key/token/password…），只做粗筛——
        真正的防线是「内容默认可以不记」（`content=False`），这里只是额外一层。
        """
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if any(hint in str(key).lower() for hint in _SENSITIVE_HINT):
                cleaned[key] = "***"
            elif isinstance(value, dict):
                cleaned[key] = self.redact(value)
            else:
                cleaned[key] = value
        return cleaned
