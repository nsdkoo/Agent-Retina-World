# Agent 运行时（harness）完善计划

项目：`D:\素材存储\Agent-Retina-World`
日期：2026-09-30
范围：**硬标准五项 + 记忆收尾回流**（用户已拍板）

---

## 一、结论速览

### 用户的四个关键词核对

| 关键词 | 判定 | 说明 |
|---|---|---|
| 记忆 | ✓ 标准要素 | 但要分三层：Session（会话）/ State（结构化状态）/ Memory（跨会话长期） |
| 工具 | ✓ 标准要素 | 2026 事实标准接口是 **MCP** |
| 审计 | ✓ 标准要素 | 要拆两块：Observability/Tracing（运行记录）+ HITL 不可变审计轨（审批人/决定/理由） |
| ReAct | **△ 部分对** | ReAct 是 2022 年的"推理+行动交替"范式，**2026 年已泛化为「LLM 驱动的工具调用循环」**，ReAct 只是其中一种形状，不再是独立必选项 |

### 术语澄清

**Agent = Model + Harness**。harness 是"模型之外的全部工程代码"。
framework 是造 harness 的库（LangGraph/AutoGen）；runtime 是让它活着的进程环境。

### 现状 vs 业界（8 维度）

| 维度 | 本项目现状 | 业界是否硬标准 | 本计划 |
|---|---|---|---|
| Tool use | **完整可用** | 硬（schema 是前置） | 只补 **#7 参数校验** |
| HITL 审批门 | **完整可用**（PolicyEngine 是唯一门卫、无绕过路径） | 硬 | ✅ **已达标，不动** |
| State/Checkpoint | 同进程完整，**跨进程未接线** | **硬** | **#1 跨进程续跑** |
| Observability | 只有事件骨架，**无 span、events 表只写不读** | **硬** | **#2 Tracing/回放** |
| Error Recovery | **只记不救**（无重试/降级/replan） | **硬** | **#4 错误恢复** |
| 取消/超时 | **完全没有** | 非硬，但 #4 依赖 | **#3 取消/超时** |
| Memory 接入 | agent 目录零引用 memory | 锦上添花 | 只做**收尾回流** |
| 统一审批 | 两套 registry 分叉 | 卫生项 | **本轮不做** |

### 一句话判断

**工具层、状态机、HITL 三块已经工程化 —— 比多数同龄项目克制。真正缺的是「状态可恢复 + 结构化 tracing + 工具可靠性」这三条硬标准。**

最大风险不是缺功能，而是**为了对标把 tracing/记忆堆过头**。

---

## 二、五项任务

### #7 参数校验（前置）· 小 · 0.5 天

**为什么先做**：`registry.run` 现在无 required/类型校验，类型错被 `except Exception` 兜成「执行失败：…」——
**重试机制无法区分「参数写错了」和「网络抖了」**，不修就是给 #4 埋雷。

**改动**
- `tools/registry.py`：`ToolSpec` 加可选 `schema: dict[str, dict] | None = None`，形如
  `{"path": {"type": "string", "required": True}, "steps": {"type": "integer"}}`。
  **不填 schema 的工具完全按现状工作**（从 `params_doc` 推断，保 `list_openai_tools` 兼容）。
- 新增 `ToolRegistry.validate(spec, params) -> (bool, str, dict)`：查未知键、必填缺失、类型（`"3"→3`、`"true"→True`）。
  `run()` 前置调用，失败即返回 `invalid_params`，不进 handler。
- `list_openai_tools`：有 schema 时生成**正确 JSON 类型 + required**（从源头减少模型传错类型）。
- **增量填 schema**：只给真会被 LLM 传参的 ~15 个工具填（`files.*` / `shell.run` / `volume.*` / `input.*` / `app.open` / `sys.open_url` / `journal.search`），其余不动。

**验收**：类型错/缺必填在 handler 之前被拦，返回 `error_kind="invalid_params"`，不再是「执行失败」。

---

### #1 跨进程续跑 · 中 · 1–1.5 天 · 最硬的一条

**为什么**：状态外部化 + 可寻址恢复是业界共识（LangGraph checkpointer / Anthropic Session / ADK SessionService）。
现在 `latest_unfinished()` 定义了却无调用方，重启后正在跑的任务直接丢。

**改动**
- `agent/state.py`：
  - `to_dict()` 补 `plan`（**当前漏了，不是无损序列化**）
  - 新增对称的 `from_dict()`，缺失键全部兜底（兼容旧行）
- `agent/trajectory.py`：`load()` 改为 `AgentState.from_dict(...)`，消除手写反序列化。
  **顺带修一个现有 bug：`load()` 丢了 `started_at/ended_at`，导致 `elapsed_ms` 永远为 0**。
- `agent/controller.py`：新增 `resume_from(task_id)`：
  - `trajectory.load` → `_rehearse(state)` → 若为 `AWAITING_*` 则**重新弹审批、不自动执行**（安全）→ 否则转 RUNNING → `_advance()`
  - `_rehearse` 重建打转检测表 `_seen`，并处理**崩溃时的残留 step**：
    - 幂等工具 → 重置 PENDING 重跑
    - 非幂等 → 如实记 `FAILED("上次执行到一半，结果未知")`，**跳过不重跑**（避免二次副作用）
- `voice/assistant.py`：`build_agent` 后读 `latest_unfinished()` 存成 `_pending_resume`，**只提示不自动跑**；
  触发词「接着做 / 继续上次」→ `resume_pending()`。

**验收**：① `from_dict(to_dict())` 逐字段相等（含时间戳）；② 新进程 `resume_from` 能从断点跑完；③ 挂起态重启后重新弹确认而非自动执行。

---

### #3 取消 / 步级超时 · 中 · 1 天 · #4 的前置

**为什么**：#4 的步级超时依赖它；且长任务目前**无法从外部打断**。

**改动**
- `controller.py`：`self._cancel = threading.Event()`（协作式，线程无法强杀）。
  `_advance` 每轮开头 + `_collect_parallel` 循环内检查 `if self._cancel.is_set()`。
- `cancel()` 语义扩展（**挂起分支保持原行为**，现有测试依赖）：
  - `waiting` → 立即 `REJECTED`（不变）
  - `running` → 置令牌，边界处停
- **新增终态 `CANCELLED`**（区别于 `REJECTED` 门口拒绝 / `STOPPED` 系统停机）：
  `state.py` 加枚举 + `_ALLOWED` 迁移 + `summary()` 分支（「已取消（走到 X/Y 步）」）。
- 步级超时：`step_timeout` 配置项（默认 **0=关**），用 `future.result(timeout=...)`。
  - 只读工具超时 → `FAILED` 不重试，继续
  - **写/非幂等工具超时 → 转 ERROR 停任务**（写动作可能半完成，不能假装无事继续）
  - **注释里写明局限**：Python 不能杀线程，超时只是"放弃等待"，底层线程可能仍在跑。
    所以默认关闭，优先透传本就支持原生超时的工具（`shell.run` 有 `timeout` 参数）。

**验收**：① 运行中另一线程 `cancel()` → 一个步边界内停下且状态为 `CANCELLED`；② 超时记 FAILED 且不重试；③ 挂起态 cancel 仍是 `REJECTED`（回归）。

---

### #2 Tracing / 审计回放 · 中 · 1 天 · 自研 + 字段对齐 OTel

**为什么**：`events` 表只写不读、无 span 概念 —— **没 trace 就无法排障、无法调优**，这是"审计"的底座。

**改动**
- 新增 `agent/trace.py`：
  ```python
  @dataclass
  class Span:
      trace_id: str          # == task_id（1 任务 = 1 trace）
      span_id: str; parent_span_id: str | None
      name: str              # "task" / "step:files.move" / "guardrail:policy"
      kind: str              # task | agent | function | guardrail | generation
      start_ts: float; end_ts: float | None; status: str = "ok"
      attributes: dict       # 低基数、无 PII
      events: list[dict]     # 承载内容：input/output/exception
  class SpanRecorder:
      def start(...) -> Span ; finish(span, status) ; event(span, name, attributes)
      def redact(payload: dict) -> dict    # 复用 capture/privacy.py 的敏感模式
  ```
- **span 层级映射到现有 `task_id + step`**：
  - `task` span：`gen_ai.operation.name="invoke_agent"`
  - `function` span（每步）：`gen_ai.operation.name="execute_tool"`、`tool.name`、`step.index`，**内容进 events**
  - `guardrail` span：`policy.decision` / `policy.mode`，理由进 events
  - `generation` span（可选，planner 的 LLM 调用）：**`gen_ai.request.model` 与 `gen_ai.response.model` 都要记**（服务端 fallback 会让两者不同）
- `trajectory.py` 加第 4 张表 `spans`（**不动 events 表结构**）+ 三个方法：
  - `read_events(task_id, kind, limit)` —— 补齐「events 只写不读」缺口
  - `replay(task_id) -> list[dict]` —— 合并 events+spans 成有序时间线
  - `export_otel(task_id) -> list[dict]` —— `{traceId, spanId, parentSpanId, name, kind, startTimeUnixNano, ..., attributes, events}`，将来接 Collector 只差一个 exporter
- **PII 防护**（照 OTel 约定）：内容只进 `span.events`，attributes 放低基数元数据；
  配置 `agent.trace_content: true|false`，false 时丢弃 events。

**验收**：① 2 步任务能 `replay` 出 1 task + 2 function + ≥2 guardrail 且父子正确；② `trace_content=false` 时库里不含 params 明文；③ `export_otel` 的 `gen_ai.*` 键齐全。

---

### #4 错误恢复 · 中偏大 · 1–1.5 天 · 依赖 #7 与 #3

**为什么**：失败只记不救。业界三条支柱：**幂等 + 有预算的重试 + 补偿**。

**改动**
- **失败分级**：`ActionResult` 加 `error_kind`（`retryable | correctable | fatal | timeout_unknown | invalid_params`）；
  `registry.run` 的 except 处按异常类型分级（`OSError/TimeoutError→retryable`；「找不到/已存在」→`correctable`；`PlatformError→fatal`）。
- **幂等字段**：`ToolSpec.idempotent: bool = False`（**默认 False=保守**）。
  只给纯读工具显式标 True（`files.list/find/read/grep/glob`、`app.list`、`journal.*`、`clip.get`、`win.list`）。
  **写工具一律不标**（`files.move` 重跑会再移一次）。
- **重试预算**：`max_retries_per_step=2`、`max_total_retries=5`（防风暴）、指数退避+jitter。
  仅当 `error_kind=="retryable"` **且** `idempotent is True` **且**未超预算才重试。
- **熔断**：同一工具本任务累计失败 ≥3 → 后续该工具前置 DENY。
- **replan**：`error_kind=="correctable"` 且还有剩余目标 且 `replans < 1`（默认 1 次）时，
  用「当前步 + 后续步的 goal」重新规划，**剔除与失败步同 signature 的新步**，splice 进 `state.steps[cursor:]`。
- **明确不做 saga 补偿**：单机桌面、文件操作已有 `files.undo` + 回收站，做补偿框架是过度工程。

**验收**：① 瞬时错误重试至多 2 次后成功；② **非幂等写失败不重试**；③ 「找不到」触发**恰好 1 次** replan；④ 同工具连挂 3 次后第 4 次被 DENY。

---

### #5 记忆收尾回流（简化版）· 小 · 0.5 天

**做什么**：任务 `FINISHED` 时把「目标 + 结果 + task_id」写一条 **episode**（情节记忆，会衰减、可检索）。

**为什么是 episode 不是 fact**：任务日志写进语义层会**污染检索**。fact 只留给用户明确纠正偏好的场景（"以后都…"）。

**关键设计**：写进**现有 `events` 表**（`page_category="桌面任务"`、`user_action="task_runner"`、`summary≤120字`、`evidence_paths=[task_id]`），
这样**现成的 `HybridRetriever` 直接能检索到，零新增检索代码**。

接线：`controller._finish` 里 best-effort 调用，**失败只记 debug，绝不阻塞收尾**。

**不做**：规划前注入上下文（agent 零引用 memory，plumbing 成本不低，而任务短、目标自足，收益小）。

**验收**：任务完成后 `memory.list_events()` 能查到 `page_category="桌面任务"` 一条。

---

## 三、执行顺序

```
#7 参数校验（0.5d）
  └→ #1 跨进程续跑（1–1.5d）
  └→ #3 取消/超时（1d）
       └→ #4 错误恢复（1–1.5d）
#2 Tracing（1d，可与上面并行）
#5 记忆收尾回流（0.5d，独立）
```

**总工作量**：约 **5–5.5 天**。

---

## 四、明确不做的事

| 不做 | 理由 |
|---|---|
| **#6 统一审批** | 核心「运行时强制审批门」已由 PolicyEngine 达标；两套 registry 是卫生问题，不是能力缺失 |
| **#5 规划前注入记忆** | 任务短、目标自足，收益小；agent 零引用 memory，plumbing 成本不低 |
| OTel SDK / exporter | 单机桌面工具，自研结构 + 字段对齐足够，将来想导出只差一个 exporter |
| saga 补偿框架 | 文件操作已有 undo + 回收站 |
| 向量记忆接 agent | 过度工程 |
| MCP 化工具层 | 自研注册表够用，改造成本远大于收益 |

### 按「假设会过时」原则的删除清单

Anthropic《Scaling Managed Agents》：「harness 里编码的假设会随模型变强而过时」。
以下是可以清掉的：

| 可删/可简化 | 理由 |
|---|---|
| `controller._signature()` 与 `_advance` 内联签名重复 | 去重，统一走 `_signature` |
| `EventStream.clear()`（无调用方） | 接上 UI「清空」或删 |
| `AgentState.plan` 与 `steps` 冗余 | 序列化补全或干脆由 steps 派生 |
| `executor._confirm_high_risk`「说两遍」启发式 | 若将来做 #6 则被 PolicyEngine 取代 |

**保留**：规则切句 planner + 打转检测（覆盖无 LLM 的离线路径，成本极低）。

---

## 五、关键文件

**改动**
- `src/screen_agent/agent/state.py`（`from_dict` / `CANCELLED` / `plan` 序列化）
- `src/screen_agent/agent/trajectory.py`（`load` 重构 / `read_events` / `spans` 表 / `replay` / `export_otel`）
- `src/screen_agent/agent/controller.py`（`resume_from` / 取消令牌 / 超时 / 重试 replan / tracer 接线）
- `src/screen_agent/tools/registry.py`（`schema` / `validate` / `idempotent` / `error_kind`）
- `src/screen_agent/tools/base.py`（`error_kind`）
- `src/screen_agent/tools/registry_setup.py`（`idempotent` / `schema` 标注）
- `src/screen_agent/voice/assistant.py`（续跑提示 / cancel 入口）
- `src/screen_agent/memory/store.py`（`save_agent_episode`）
- `tests/test_agent_core.py`（全部新增测试）
- `config.yaml` / `config.example.yaml`（`agent.resume_on_start` / `max_retries` / `step_timeout_seconds` / `trace_content`）

**新增**
- `src/screen_agent/agent/trace.py`

---

## 六、每项完成标准（沿用今天的节奏）

- 代码改完，改动点与本计划一致
- 新增测试通过
- **全量回归通过**（当前基线 215 例，只增不减）
- 实测数据对比（改进前 vs 改进后）
- 提交一条 commit

任一项不达标就停下修，不带病进入下一项。
