"""Agent 运行时：事件流 + 任务状态机 + 规划 + 权限策略 + 主循环 + 轨迹持久化。

分层与 Pi 的 pi-agent-core → pi-coding-agent 一个思路：底下这几个模块各自只管一件事，
谁都不依赖 UI；`build_agent` 负责把它们装配成一个能直接用的 AgentController。
"""

from __future__ import annotations

from pathlib import Path

from screen_agent.agent.controller import AgentController
from screen_agent.agent.events import Event, EventStream, EventType
from screen_agent.agent.planner import Planner, split_clauses
from screen_agent.agent.policy import (
    MODE_LABELS,
    Decision,
    PermissionMode,
    PolicyEngine,
    ToolPermission,
)
from screen_agent.agent.state import (
    AgentState,
    IllegalTransition,
    PlanStep,
    StepRecord,
    StepStatus,
    TaskState,
)
from screen_agent.agent.trajectory import TrajectoryStore

__all__ = [
    "AgentController", "AgentState", "Decision", "Event", "EventStream", "EventType",
    "IllegalTransition", "MODE_LABELS", "PermissionMode", "PlanStep", "Planner",
    "PolicyEngine", "StepRecord", "StepStatus", "TaskState", "ToolPermission",
    "TrajectoryStore", "build_agent", "split_clauses",
]


def build_agent(
    project_root: Path,
    chat_client=None,  # noqa: ANN001
    app_aliases: dict[str, str] | None = None,
    url_aliases: dict[str, str] | None = None,
    mode: str = "smart",
    max_steps: int = 5,
    enabled: bool = True,
    journal=None,  # noqa: ANN001 - DesktopJournal，注册桌面行为查询工具用
    memory_store=None,  # noqa: ANN001 - MemoryStoreV2，任务收尾回流记忆用
    agent_cfg: dict | None = None,
) -> AgentController | None:
    """装配一个可用的 AgentController；enabled=False 时返回 None，让调用方走老路径。"""
    if not enabled:
        return None

    from screen_agent.tools.registry_setup import build_default_registry

    root = Path(project_root)
    cfg = agent_cfg or {}
    # 审批统一由 PolicyEngine 负责，registry 这边不再挂 confirm_fn——两道确认会互相打架
    registry = build_default_registry(journal=journal)
    policy = PolicyEngine(
        mode=mode,
        store_path=root / "data" / "agent" / "policy.json",
    )
    # 规划用的记忆片段：让 planner 知道「这件事用户平时是怎么干的」。
    # 取不到就返回空串，planner 那边会整段跳过——**不能因为记忆没接上就把规划也停了**
    context_fn = None
    if memory_store is not None:
        try:
            from screen_agent.memory.assembler import ContextAssembler

            context_fn = ContextAssembler(memory_store).build_agent_context
        except Exception:  # noqa: BLE001
            context_fn = None

    planner = Planner(
        registry,
        chat_client=chat_client,
        app_aliases=app_aliases or {},
        url_aliases=url_aliases or {},
        max_steps=max_steps,
        context_fn=context_fn,
    )
    # 收尾回流记忆的出口。用鸭子类型而不是直接依赖 memory 模块——
    # 没接记忆库时（比如测试）整条链路照常工作，只是不写 episode
    sink = None
    if memory_store is not None and hasattr(memory_store, "save_agent_episode"):
        sink = memory_store.save_agent_episode

    return AgentController(
        registry=registry,
        planner=planner,
        policy=policy,
        stream=EventStream(),
        trajectory=TrajectoryStore(root / "data" / "agent" / "tasks.db"),
        step_timeout=float(cfg.get("step_timeout_seconds", 0) or 0),
        trace_content=bool(cfg.get("trace_content", True)),
        max_retries_per_step=int(cfg.get("max_retries", 2) or 0),
        episode_sink=sink,
    )
