"""Agent 主循环：规划 → 逐步执行 → 审批挂起 → 恢复 → 汇总。

对齐四家的做法，各取所长：

- **OpenHands**：状态机 + Action/Observation 事件对。控制器只负责驱动循环，不替 Agent 决策；
  状态迁移显式声明，挂起时不销毁现场
- **block/goose**：动手前必过权限关（PolicyEngine），危险动作一律挂起等确认
- **Cline**：Plan 与 Act 分离，计划要给人看；任务状态落盘，进程重启可续跑
- **Pi**：事件流即审计轨迹；Steering / Follow-up 双队列区分「改变方向」和「待会儿再做」；
  独立只读步骤并行、有副作用的串行；同一动作重复多次即判定在原地打转

三条不变量：
1. 任何一步执行前必过 `policy.judge`，没有绕过路径
2. 挂起不丢现场：游标停在原地，`resume` 从原处接着走
3. 失败不吞：单步失败如实记 FAILED 并继续，最后汇总里报出来
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime

from screen_agent.agent.events import Event, EventStream, EventType
from screen_agent.agent.planner import Planner
from screen_agent.agent.policy import Decision, PolicyEngine, ToolPermission
from screen_agent.agent.state import AgentState, StepStatus, TaskState
from screen_agent.agent.trace import Span, SpanRecorder
from screen_agent.agent.trajectory import TrajectoryStore
from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel, ToolRegistry

logger = logging.getLogger(__name__)

# 用户对挂起问题的回答（本机人说人话，别指望关键词命中率 100%，宁可漏判也不算错）
_CONFIRM_WORDS = ("继续", "确认", "执行", "可以", "好", "是的", "对", "嗯", "动手", "接着", "ok", "OK")
_SKIP_WORDS = ("跳过", "略过", "下一步", "不管这步", "略")
_CANCEL_WORDS = ("取消", "算了", "不做了", "不做", "停", "别做", "放弃", "不用了")

# 已经真正结束、不该再续跑的状态。这是 trajectory 里 `_UNFINISHED` 的补集——
# ERROR 不在其中：它是可以续的（问题解决了就能接着跑）。
_TERMINAL = {TaskState.FINISHED, TaskState.REJECTED, TaskState.STOPPED}


class AgentController:
    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        policy: PolicyEngine | None = None,
        stream: EventStream | None = None,
        trajectory: TrajectoryStore | None = None,
        max_iterations: int = 8,
        max_parallel: int = 3,
        max_step_repeats: int = 2,
        step_timeout: float = 0.0,
        tracer: SpanRecorder | None = None,
        trace_content: bool = True,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.policy = policy or PolicyEngine()
        self.stream = stream or EventStream()
        self.trajectory = trajectory
        self.max_iterations = max_iterations
        self.max_parallel = max_parallel
        self.max_step_repeats = max_step_repeats
        self.step_timeout = step_timeout

        self._state: AgentState | None = None
        self._seen: dict[str, int] = {}       # (tool, params) → 出现次数，防原地打转
        self._follow_ups: deque[str] = deque()  # Pi 式 Follow-up：做完了接着做
        # 协作式取消令牌。Python 没法强杀线程，只能把令牌立起来、
        # 在**步边界**检查——所以取消不是即时的，是"这一步跑完就停"
        self._cancel = threading.Event()
        # trace：一次任务 = 一条 trace（trace_id 就是 task_id），
        # span 全攒在内存里，收尾时一次性落库——每步都写盘太吵
        self.tracer = tracer or SpanRecorder(content=trace_content)
        self._spans: list[Span] = []
        self._task_span: Span | None = None

    # ---- 只读视图 ----

    @property
    def state(self) -> AgentState | None:
        return self._state

    @property
    def is_waiting(self) -> bool:
        return self._state is not None and self._state.state in (
            TaskState.AWAITING_USER_CONFIRMATION,
            TaskState.AWAITING_USER_INPUT,
        )

    @property
    def is_running(self) -> bool:
        return self._state is not None and self._state.state in (
            TaskState.PENDING, TaskState.PLANNING, TaskState.RUNNING,
        )

    # ---- 入口 ----

    def run(self, goal: str) -> ActionResult:
        """跑一个新任务；队列里攒着 Follow-up 的话，跑完自动接着下一个。"""
        try:
            result = self._run_once(goal)
            while not self.is_waiting and self._follow_ups:
                nxt = self._follow_ups.popleft()
                tail = self._run_once(nxt)
                result = ActionResult(
                    success=result.success and tail.success,
                    message=f"{result.message}\n{tail.message}",
                    detail={"chained": True},
                    options=tail.options,
                )
            return result
        finally:
            # 不管走哪条路径（正常结束、规划失败、挂起）都把 trace 收掉，
            # 否则半截 trace 在回放里看不出任务到底经历了什么
            self._close_trace()
            self._flush_spans()

    # ---- 续跑（跨进程） ----

    def resume_from(self, task_id: str) -> ActionResult | None:
        """把库里没跑完的任务读回来接着跑。

        对标 LangGraph 的 `Command(resume=...)`：状态外部化之后，恢复不需要
        原来是哪个进程——新进程拿着 task_id 就能接上。

        返回 None 表示任务不存在或已经结束了。
        """
        if self.trajectory is None:
            return None
        state = self.trajectory.load(task_id)
        if state is None or state.state in _TERMINAL:
            return None

        self._state = state
        self._rehearse(state)
        self._cancel.clear()      # 续跑是新的一次执行，别带着上次的取消令牌
        self._start_trace(state.goal, state)   # 续跑也开一条 trace，便于对比两次执行
        self._publish(
            EventType.USER_MESSAGE,
            {"text": f"（续跑）{state.goal}", "resume": True},
            state,
        )

        # 挂起态：把问题重新抛给用户，**不自动往下执行**——
        # 上次就是需要确认才停下的，重启不该被当成"默认同意"
        if state.state in (
            TaskState.AWAITING_USER_CONFIRMATION, TaskState.AWAITING_USER_INPUT,
        ):
            return self._resume_pending_ask(state)

        if state.state is not TaskState.RUNNING:
            self._transition(state, TaskState.RUNNING)
            if state.state is not TaskState.RUNNING:
                # 状态机不允许（比如库里的状态已经和 steps 对不上）：
                # 以"能继续干活"为准，不因为一个状态名把任务判死
                state.state = TaskState.RUNNING
        self._persist(state)
        return self._advance()

    def _rehearse(self, state: AgentState) -> None:
        """恢复现场：重建打转检测表，并处理崩溃时留下的残步。

        **残步是这里最需要小心的地方**——进程是在某一步执行到一半时被杀的，
        那一步到底做没做成功是未知的：
        - 幂等工具（纯读）→ 重跑没有风险，重置回 PENDING
        - 非幂等工具（写）→ **绝不重跑**，如实记失败并跳过。
          宁可少做一步，也不能因为"重试"把文件移两次。
        """
        self._seen.clear()
        for step in state.steps:
            if step.status in (StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED):
                signature = self._signature(step)
                self._seen[signature] = self._seen.get(signature, 0) + 1

        current = state.current_step()
        if current is None or current.status is not StepStatus.RUNNING:
            return
        spec = self.registry.get(current.tool)
        if spec is not None and spec.idempotent:
            current.status = StepStatus.PENDING
            current.observation = ""
        else:
            self._mark(
                current, StepStatus.FAILED,
                "上次执行到一半就中断了，结果未知，没有重跑", False,
            )
            state.cursor += 1

    def _resume_pending_ask(self, state: AgentState) -> ActionResult:
        """重启后把挂起的问题重新抛一遍（不自动执行）。"""
        step = state.current_step()
        if step is None:
            state.fail("挂起的步骤已经不在了")
            self._persist(state)
            return ActionResult(success=False, message="这个任务的状态对不上，我给它标成中断了")
        return self._suspend(step, "上次就是卡在这一步等你确认（重启后续跑）")

    # ---- 主循环 ----

    def _start_trace(self, goal: str, state: AgentState) -> None:
        """开一条新 trace。一次任务 = 一条 trace，trace_id 直接用 task_id。"""
        self._spans = []
        self._task_span = self.tracer.agent_span(goal, trace_id=state.task_id)
        self._spans.append(self._task_span)

    def _flush_spans(self) -> None:
        """把攒下的 span 落库。可观测性失败不能影响任务本身的收尾。"""
        if self.trajectory is None or not self._spans:
            return
        try:
            self.trajectory.save_spans(self._spans)
        except Exception:  # noqa: BLE001
            logger.debug("trace 落盘失败", exc_info=True)

    def _run_once(self, goal: str) -> ActionResult:
        state = AgentState(goal=goal)
        self._state = state
        self._seen.clear()
        self._cancel.clear()
        self._start_trace(goal, state)
        self._publish(EventType.USER_MESSAGE, {"text": goal}, state)

        self._transition(state, TaskState.PLANNING)
        try:
            steps = self.planner.plan(goal)
        except Exception as exc:  # noqa: BLE001 - 规划器异常不该炸掉整个助手
            logger.exception("规划失败")
            state.fail(f"规划失败：{exc}")
            self._persist(state)
            return ActionResult(success=False, message=f"拆解任务时出错：{exc}")

        if not steps:
            state.fail("拆不出可执行的步骤")
            self._persist(state)
            return ActionResult(
                success=False,
                message=f"「{goal}」我没拆成可执行的步骤，你可以说得更具体一点",
            )

        state.load_plan(steps)
        self._publish(EventType.PLAN, {
            "count": len(steps),
            "steps": [{"goal": s.goal, "tool": s.tool, "why": s.why} for s in steps],
        }, state)
        self._transition(state, TaskState.RUNNING)
        self._persist(state)
        return self._advance()

    # ---- 主循环 ----

    def _advance(self) -> ActionResult:
        state = self._state
        assert state is not None
        while True:
            # 取消只在**步边界**生效：当前这步已经发出去了，硬切会把副作用留在半路
            if self._cancel.is_set():
                return self._cancel_run()
            step = state.current_step()
            if step is None:
                return self._finish()

            if state.iteration >= self.max_iterations:
                state.error = f"已达迭代上限 {self.max_iterations} 步"
                self._transition(state, TaskState.STOPPED)
                self._persist(state)
                return ActionResult(
                    success=False,
                    message=f"这活儿步骤太多，我先停下（{state.done_count()}/{len(state.steps)} 步完成）。"
                            f"可以把目标拆小一点再让我做",
                )
            state.iteration += 1

            spec = self.registry.get(step.tool)
            if spec is None:
                self._mark(step, StepStatus.SKIPPED, f"没有这个工具：{step.tool}", False)
                state.cursor += 1
                self._persist(state)
                continue

            signature = f"{step.tool}:{sorted(step.params.items())}"
            self._seen[signature] = self._seen.get(signature, 0) + 1
            if self._seen[signature] > self.max_step_repeats:
                # Pi 式打转检测：同一个动作来回做，基本是计划坏了
                self._mark(step, StepStatus.SKIPPED, "这步和前面重复，跳过", False)
                state.cursor += 1
                self._persist(state)
                continue

            decision, reason = self.policy.judge(spec, step.params)
            self._record_guardrail(step, decision, reason)
            if decision is Decision.DENY:
                self._mark(step, StepStatus.SKIPPED, reason or "被策略拒绝", False)
                state.cursor += 1
                self._persist(state)
                continue
            if decision is Decision.ASK:
                return self._suspend(step, reason)

            # 连续的只读步骤攒一批并行跑（Pi 默认并行工具执行；写操作仍串行）
            group = self._collect_parallel(state)
            if len(group) > 1:
                self._run_parallel(group)
            else:
                self._run_one(step)
            continue

    def _signature(self, step) -> str:
        return f"{step.tool}:{sorted(step.params.items())}"

    def _collect_parallel(self, state: AgentState) -> list:
        """把当前步后面连续的一串「SAFE + 放行」步骤也收进来一起并行。

        写操作一律不并；签名已经出现过 max_step_repeats 次的也不并——
        否则并行这条路径会绕开 _advance 里的打转检测。
        """
        current = state.current_step()
        group = [current] if current is not None else []
        index = state.cursor + 1
        while index < len(state.steps) and len(group) < self.max_parallel:
            candidate = state.steps[index]
            spec = self.registry.get(candidate.tool)
            if spec is None or spec.risk is not RiskLevel.SAFE:
                break
            decision, _ = self.policy.judge(spec, candidate.params)
            if decision is not Decision.ALLOW:
                break
            signature = self._signature(candidate)
            if self._seen.get(signature, 0) >= self.max_step_repeats:
                break
            self._seen[signature] = self._seen.get(signature, 0) + 1
            group.append(candidate)
            index += 1
        return group

    def _run_parallel(self, group: list) -> None:
        state = self._state
        assert state is not None
        with ThreadPoolExecutor(max_workers=len(group)) as pool:
            futures = {pool.submit(self._invoke, step): step for step in group}
            for future in futures:
                step = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = ActionResult(success=False, message=str(exc))
                self._settle(step, result)
        state.cursor += len(group)
        self._persist(state)

    def _record_guardrail(self, step, decision, reason: str) -> None:  # noqa: ANN001
        """把权限判定也记成 span。

        审批门是运行时强制的，每次判定都该留痕——出问题时
        「为什么这步被拦了」和「为什么这步没拦」是最常要回答的两个问题。
        """
        if self._task_span is None:
            return
        span = self.tracer.guardrail_span(
            decision.value, reason,
            trace_id=self._task_span.trace_id, parent=self._task_span,
            mode=self.policy.mode.value, step_index=step.index,
        )
        self.tracer.finish(span)
        self._spans.append(span)

    def _run_one(self, step) -> None:
        state = self._state
        assert state is not None
        result = self._invoke(step)
        self._settle(step, result)
        state.cursor += 1
        self._persist(state)

    def _invoke(self, step) -> ActionResult:
        self._publish(EventType.ACTION, {
            "goal": step.goal, "tool": step.tool, "params": step.params, "why": step.why,
        }, self._state, step.index + 1)
        span = self._open_tool_span(step)
        if self.step_timeout and self.step_timeout > 0:
            result = self._invoke_with_timeout(step)
        else:
            try:
                result = self.registry.run(step.tool, **step.params)
            except Exception as exc:  # noqa: BLE001 - 工具层异常统一转成失败观察
                logger.exception("工具执行异常：%s", step.tool)
                result = ActionResult(success=False, message=f"执行失败：{exc}")
        self._close_tool_span(span, result)
        return result

    def _open_tool_span(self, step) -> Span | None:  # noqa: ANN001
        if self._task_span is None:
            return None
        spec = self.registry.get(step.tool)
        span = self.tracer.tool_span(
            step.tool, step.params,
            trace_id=self._task_span.trace_id, parent=self._task_span,
            step_index=step.index, risk=spec.risk.name if spec is not None else "",
        )
        self._spans.append(span)
        return span

    def _close_tool_span(self, span: Span | None, result: ActionResult) -> None:
        """收 span。内容（输出摘要）进 events，不进 attributes。"""
        if span is None:
            return
        self.tracer.event(span, "output", {
            "result.message": (result.message or "")[:200],
            "success": bool(result.success),
            "error_kind": result.error_kind,
        })
        self.tracer.finish(span, "ok" if result.success else "error")

    def _invoke_with_timeout(self, step) -> ActionResult:
        """带超时执行一步。

        **局限要说清楚**：Python 杀不掉线程，这里的超时只是"不再等它"，
        底层那次调用可能还在跑（孤儿线程）。所以默认是关的（`step_timeout=0`），
        能用工具自带超时的（比如 `shell.run` 的 `timeout` 参数）优先用工具自己的。

        因为副作用状态未知，超时的错误分类按工具是否幂等来分：
        只读超时归 `retryable`（重跑无害），写操作归 `timeout_unknown`
        （**不能被当成普通失败重试**——它可能已经改了东西）。
        """
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(self.registry.run, step.tool, **step.params)
            return future.result(timeout=self.step_timeout)
        except FutureTimeout:
            spec = self.registry.get(step.tool)
            idempotent = bool(spec is not None and spec.idempotent)
            return ActionResult(
                success=False,
                message=f"这步超过 {self.step_timeout:g} 秒还没返回，先不等了",
                detail={"timeout": True, "tool": step.tool},
                error_kind="retryable" if idempotent else "timeout_unknown",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("工具执行异常：%s", step.tool)
            return ActionResult(success=False, message=f"执行失败：{exc}")
        finally:
            # wait=False：不等可能还活着的孤儿线程，否则超时形同虚设
            pool.shutdown(wait=False)

    def _settle(self, step, result: ActionResult) -> None:
        step.ended_at = datetime.now()
        self._mark(
            step,
            StepStatus.DONE if result.success else StepStatus.FAILED,
            result.message,
            result.success,
        )
        self._publish(EventType.OBSERVATION, {
            "goal": step.goal, "tool": step.tool,
            "success": result.success, "message": result.message,
            "elapsed_ms": step.elapsed_ms,
        }, self._state, step.index + 1)

    # ---- 挂起与恢复 ----

    def _suspend(self, step, reason: str) -> ActionResult:
        state = self._state
        assert state is not None
        step.status = StepStatus.AWAITING
        self._transition(state, TaskState.AWAITING_USER_CONFIRMATION)
        question = f"第 {step.index + 1} 步：{step.goal}。{reason or '要你点头才动手'}"
        options = ["继续执行", "跳过这一步", "取消任务"]
        self._publish(EventType.ASK, {
            "question": question, "options": options,
            "tool": step.tool, "params": step.params,
        }, state, step.index + 1)
        self._persist(state)
        return ActionResult(success=True, message=question, options=options, detail={"ask": True})

    def try_resume(self, text: str) -> ActionResult | None:
        """挂起状态下先把用户这句话当回答猜一次；猜不中返回 None，交回上层正常路由。

        Pi 的双队列语义在这里落地：
        - 答「继续 / 跳过 / 取消」→ 回答当前问题
        - 说别的（看起来是新指令）→ 判定为 Steering，打断当前任务并让上层按新指令走
        """
        if not self.is_waiting:
            return None
        state = self._state
        assert state is not None
        answer = (text or "").strip()
        if not answer:
            return None

        if any(w in answer for w in _CANCEL_WORDS):
            return self.cancel()
        if any(w in answer for w in _SKIP_WORDS):
            step = state.current_step()
            if step is not None:
                self._mark(step, StepStatus.SKIPPED, "按你的意思跳过", False)
                state.cursor += 1
            self._transition(state, TaskState.RUNNING)
            self._persist(state)
            return self._advance()
        if any(w in answer for w in _CONFIRM_WORDS):
            step = state.current_step()
            if step is None:
                self._transition(state, TaskState.RUNNING)
                return self._finish()
            self._transition(state, TaskState.RUNNING)
            result = self._invoke(step)
            self._settle(step, result)
            state.cursor += 1
            self._persist(state)
            return self._advance()

        # 不是回答——按 Steering 处理：停掉当前任务，把控制权交回上层
        self._mark_waiting_step_as_skipped()
        state.error = "用户改变了方向"
        if state.can_transition(TaskState.STOPPED):
            self._transition(state, TaskState.STOPPED)
        self._persist(state)
        return None

    def _mark_waiting_step_as_skipped(self) -> None:
        step = self._state.current_step() if self._state else None
        if step is not None and step.status is StepStatus.AWAITING:
            self._mark(step, StepStatus.SKIPPED, "你换方向了，这步没做", False)

    def follow_up(self, text: str) -> None:
        """Pi 式 Follow-up：当前任务做完之后再接着做这件事。"""
        if text.strip():
            self._follow_ups.append(text.strip())

    def cancel(self) -> ActionResult:
        """用户中途叫停。

        分两种情况，语义不同：
        - **挂起中**（在门口等确认）→ 直接判 REJECTED，任务从没真正跑起来
        - **运行中** → 立起取消令牌，等当前步跑完在边界处停，状态进 CANCELLED

        运行中的取消不是即时的——Python 杀不掉线程，硬切会把写到一半的副作用留下。
        """
        state = self._state
        if state is None:
            return ActionResult(success=False, message="现在没有在跑的任务")

        if self.is_waiting:
            self._mark_waiting_step_as_skipped()
            self._transition(state, TaskState.REJECTED)
            self._persist(state)
            self._publish(EventType.FINISH, {"summary": state.summary()}, state)
            return ActionResult(success=True, message=state.summary())

        if self.is_running:
            self._cancel.set()
            return ActionResult(success=True, message="好，这一步跑完就停")

        return ActionResult(success=False, message="现在没有在跑的任务")

    def _cancel_run(self) -> ActionResult:
        """在步边界把任务停下。当前步标成跳过并说明是用户叫停的，不是失败。"""
        state = self._state
        assert state is not None
        step = state.current_step()
        if step is not None and step.status in (StepStatus.PENDING, StepStatus.RUNNING):
            self._mark(step, StepStatus.SKIPPED, "你叫停了，这步没做", False)
            state.cursor += 1
        self._transition(state, TaskState.CANCELLED)
        self._persist(state)
        summary = state.summary()
        self._publish(EventType.FINISH, {"summary": summary, "cancelled": True}, state)
        self._close_trace("cancelled")
        self._flush_spans()
        return ActionResult(success=False, message=summary, detail={"cancelled": True})

    # ---- 收尾 ----

    def _finish(self) -> ActionResult:
        state = self._state
        assert state is not None
        done, total = state.done_count(), len(state.steps)
        failed = state.failed_steps()
        lines = [f"做完了 {done}/{total} 步："]
        for step in state.steps:
            mark = {"done": "✓", "failed": "✗", "skipped": "–"}.get(step.status.value, "·")
            detail = (step.observation or "").split("\n")[0][:40]
            lines.append(f"  {mark} {step.goal}{('：' + detail) if detail else ''}")
        summary = "\n".join(lines)
        if failed:
            summary += f"\n有 {len(failed)} 步没成，可以单独让我重做"
        state.finish(summary)
        self._publish(EventType.FINISH, {"summary": summary}, state)
        self._persist(state)
        self._close_trace("ok")
        self._flush_spans()
        return ActionResult(success=not failed, message=summary, detail={"task_id": state.task_id})

    def _close_trace(self, status: str = "ok") -> None:
        """给任务级 span 收尾。没收尾的 span 在回放里看不出这任务到底成没成。"""
        if self._task_span is not None and self._task_span.end_ts is None:
            self.tracer.finish(self._task_span, status)

    # ---- 事件与持久化 ----

    def _publish(self, kind: EventType, payload: dict, state: AgentState | None, step: int = 0) -> None:
        event = Event(
            type=kind, payload=payload,
            task_id=state.task_id if state else "", step=step,
        )
        self.stream.publish(event)
        if self.trajectory is not None:
            self.trajectory.log_event(event)

    def _transition(self, state: AgentState, target: TaskState) -> None:
        if state.state is target:
            # 原地不动不算迁移。续跑挂起任务时会再抛一次同样的问，
            # 那时状态本来就是 AWAITING，不该被当成非法跳转刷一堆警告
            return
        try:
            previous, current = state.transition(target)
        except Exception:  # noqa: BLE001 - 非法跳转不该掀翻主循环
            logger.warning("非法状态迁移：%s → %s", state.state.value, target.value)
            return
        self._publish(EventType.STATE_CHANGED, {
            "from": previous.value, "to": current.value,
        }, state)

    def _persist(self, state: AgentState) -> None:
        if self.trajectory is not None:
            try:
                self.trajectory.save(state)
            except Exception:  # noqa: BLE001 - 落盘失败不影响本次执行
                logger.debug("轨迹落盘失败", exc_info=True)

    @staticmethod
    def _mark(step, status: StepStatus, observation: str, success: bool) -> None:
        step.status = status
        step.observation = observation or ""
        step.success = success
        if step.ended_at is None:
            step.ended_at = datetime.now()

    # ---- 权限快捷操作（UI / 意图层用） ----

    def allow_always(self, tool_name: str) -> None:
        self.policy.remember(tool_name, ToolPermission.ALWAYS_ALLOW)

    def mode_label(self) -> str:
        from screen_agent.agent.policy import MODE_LABELS

        return MODE_LABELS.get(self.policy.mode, self.policy.mode.value)
