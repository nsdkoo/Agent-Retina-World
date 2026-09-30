"""Agent 事件流：Action 与 Observation 的发布订阅总线。

对标 OpenHands 的 EventStream——任何组件都能往流里发事件，也能订阅自己关心的事件类型，
AgentController 与 UI 都只是订阅者。执行逻辑和展示因此彻底解耦：加一个订阅者就能多一路
观测（日志、悬浮球状态、未来的 Web 面板），不用碰主循环。

事件最小集：
- user_message   用户指令
- plan           规划结果（步骤列表）
- action         一次工具调用（工具名、参数、风险级）
- observation    工具执行结果（成败、耗时、消息）
- ask            需要用户拍板（含候选）
- state_changed  状态机迁移
- finish         任务收尾（总结）
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    PLAN = "plan"
    ACTION = "action"
    OBSERVATION = "observation"
    ASK = "ask"
    STATE_CHANGED = "state_changed"
    FINISH = "finish"


@dataclass
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    step: int = 0
    ts: float = field(default_factory=time.time)

    def brief(self) -> str:
        """一行摘要：日志与 UI 进度条都吃这个，别让上层各自拼字符串。"""
        data = self.payload
        if self.type == EventType.USER_MESSAGE:
            return f"收到指令：{data.get('text', '')[:40]}"
        if self.type == EventType.PLAN:
            return f"拆成 {data.get('count', 0)} 步"
        if self.type == EventType.ACTION:
            return f"第 {self.step} 步 · {data.get('goal') or data.get('tool', '')}"
        if self.type == EventType.OBSERVATION:
            mark = "✓" if data.get("success") else "✗"
            return f"{mark} {str(data.get('message', ''))[:50]}"
        if self.type == EventType.ASK:
            return f"等你拍板：{str(data.get('question', ''))[:40]}"
        if self.type == EventType.STATE_CHANGED:
            return f"状态 {data.get('from')} → {data.get('to')}"
        if self.type == EventType.FINISH:
            return str(data.get("summary", "任务结束"))[:60]
        return self.type.value


class EventStream:
    """线程安全事件总线：publish 派发给订阅者，history 留轨迹供回放与排障。"""

    def __init__(self, max_history: int = 500) -> None:
        self._subs: dict[EventType | None, list[Callable[[Event], None]]] = {}
        self._history: list[Event] = []
        self._max_history = max_history
        self._lock = threading.Lock()

    def subscribe(
        self, kind: EventType | None, callback: Callable[[Event], None]
    ) -> Callable[[], None]:
        """订阅某类事件；kind=None 表示订阅全部。返回取消订阅的闭包。"""
        with self._lock:
            self._subs.setdefault(kind, []).append(callback)

        def unsubscribe() -> None:
            with self._lock:
                callbacks = self._subs.get(kind, [])
                if callback in callbacks:
                    callbacks.remove(callback)

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                del self._history[: len(self._history) - self._max_history]
            callbacks = list(self._subs.get(event.type, ())) + list(self._subs.get(None, ()))
        for callback in callbacks:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - 订阅者出错不能拖垮主循环
                logger.debug("事件订阅者异常", exc_info=True)

    def history(self, kind: EventType | None = None) -> list[Event]:
        with self._lock:
            items = list(self._history)
        return [e for e in items if kind is None or e.type == kind]

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
