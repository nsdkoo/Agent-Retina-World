# Agent-Retina-World 记忆系统 v2 设计

> 对标 Letta / Mem0 / Zep / Generative Agents，2026-09-29 深度调研后重设计。
> 本文是评估 + 架构 + 路线图，代码见 `src/screen_agent/memory/`。

## 一、现状审计（诚实版）

| 维度 | 现状 v1 | 评价 |
|---|---|---|
| 记忆分层 | 单一 `events` 情节表 + 内存里的 `chat_history` | ❌ 无语义层、无工作记忆持久化 |
| 持久化 | chat_history 重启即丢 | ❌ 顶级系统最基本要求都没达到 |
| 检索 | `list_events(limit=N)` 纯时间序取最近 N 条 | ❌ "最近≠相关"，无打分 |
| 写入 | 聚合事件全量落库，无门禁 | ⚠️ 无重要性区分，检索时被噪声淹没 |
| 反思固化 | 日报生成后不再吸收，记忆不增长 | ❌ 没有学习回路 |
| 时间感知 | 无衰减、无新旧对账 | ⚠️ 旧偏好和新偏好会同时被检索出来 |
| 上下文装配 | system prompt 塞"最近 3 条活动"一坨 | ❌ 无预算管理、无相关性选择 |

## 二、对标结论（2026 年 6–9 月最新进展 + 三大平台 + 经典论文）

### 平台基线

| 系统 | 核心机制 | LongMemEval | 可借鉴点 |
|---|---|---|---|
| **Letta**（MemGPT 系） | OS 分层：core（在上下文）/ recall（可检索）/ archival（归档），agent 自己决定升降级 | 83.2% | 分层模型 + 上下文预算意识 |
| **Mem0** | 向量 + LLM 提取；**update-in-place 对账**（新事实与旧记忆调和更新，不是追加） | ~49%（自报 94.4 待考） | 写入时对账，避免陈旧偏好污染检索 |
| **Zep/Graphiti** | 时序知识图谱：事实带 valid_at/invalid_at，能答"Q1 时什么是真的" | 63.8% | 时间有效性窗口；轻量版 = last_seen + 新旧对账 |
| **Generative Agents** | 检索分 = recency × importance × relevance；反思把低层记忆固化成高层 | 经典 | 三维打分 + 反思固化，最适 Offline 落地 |

### 2026 年 6–9 月最新研究（本轮新增对标）

| 方向 | 代表工作 | 核心发现 | 本项目落点 |
|---|---|---|---|
| **睡眠门控固化** | Sleep-Gated Dream Consolidation（6 月）；Google/Cornell Sleep 范式（arXiv 2606.03979） | 固化只在"睡眠态"跑，绝不与在线推理抢资源；醒/睡交替是持续学习生命周期 | **P0**：agent 空闲（idle 超阈值）时才触发固化线程，交互零争用 |
| **梦境重组** | Discovery by Dreaming（arXiv 2607.16256，UChicago/Stanford） | 固化的价值在跨域重组不在重放：符号系统 +21pp | P2：固化时对不相关记忆做配对重组，LLM 找连接（研究项） |
| **类型条件衰减** | ScrubJay-MEM（arXiv 2608.04746，8 月） | 每条记忆按类型给不同衰减系数（偏好衰减慢、临时事实快）；消融实验性能塌 5.7 倍 → 类型衰减是必要条件 | **P0**：facts 按 category 给半衰期（profile 30 天 / preference 14 天 / project 7 天 / entity 3 天），检索排序用 confidence × 衰减 |
| **来源溯源** | Agent Zero Memory（arXiv 2608.29606，8 月 30）LongMemEval 95.6% | 三结构并行 + **每条事实必须带来源/时间/证据指针**，宁可拒答不编造 | **P0**：facts 加 evidence 字段（来源轮次/事件 id），写库强制带溯源 |
| **记忆操作系统化** | MemOS（MemTensor，7–8 月） | 反馈回填早期步骤 → 更新的是记忆与技能不是模型权重 | P1：反思回填调 importance 权重 |
| **前瞻记忆** | Typed Intention Stores（9 月） | 记住"将来要做的事"是独立能力 | P2：待办意图表 + 到点主动提醒 |

**判断**：本项目是单机离线桌面 agent，不该照搬任一整机（Zep 要图数据库、Letta 要运行时全家桶）。正确姿势是**把四家各自的单点机制拆开，取 Offline 可实现的最小完备集**：

1. Letta 的**分层**（core / episodic / semantic）
2. Mem0 的**写入对账**（同义新旧事实更新不追加）
3. Zep 的**时间意识**（last_seen + 置信度衰减，P1 上 valid 窗口）
4. Generative Agents 的**三维检索打分 + 反思固化**

## 三、目标架构（P0 已落地）

```
                    ┌─────────────────────────────┐
   屏幕流 → 聚合 →  │  MemoryStore v2 (SQLite)     │
                    │  ├─ events    情节记忆 + importance │
                    │  ├─ facts     语义记忆（画像/偏好/项目）│
                    │  └─ sessions+session_turns  工作记忆（持久化）│
                    └──────┬──────────────┬───────┘
                           │ 写入对账        │ 三维打分检索
                  Consolidator v1      HybridRetriever
                  (规则提取+LLM hook)   recency×importance×relevance
                           │                │
                           └───────┬────────┘
                                  ▼
                          ContextAssembler
                    [画像facts] + [相关episodes] + [working turns]
                                  │ token 预算
                                  ▼
                          Chat system prompt
```

### 1. 分层（Letta 思路的离线版）

- **工作记忆**：会话轮次持久化到 `session_turns`，重启后可恢复上次对话；容量按轮次 + 字符预算双限。
- **情节记忆**：现有 `events` 表 + `importance` 字段（0-3 启发式：行为专注度、时长、场景类型）。
- **语义记忆**：`facts` 表——画像（名字/身份）、偏好（喜欢/常用）、项目（在做什么）、实体。带 `confidence` 与 `last_seen_at`。

### 2. 三维检索（Generative Agents 公式）

`score = 0.4·recency + 0.3·importance + 0.3·relevance`

- recency：指数衰减，半衰期 48h
- importance：写入时打分，检索时归一化
- relevance：查询词与摘要的 CJK bigram + 英文词重叠（向量检索留接口，P1 接入现有 embedding 配置）

### 3. 写入对账（Mem0 思路的轻量版）

写 fact 前查同 `category` 下内容归一化相同/高度相似的条目：**更新 `last_seen_at` + 提升 confidence，不追加**。避免"用户说过喜欢 A"和"用户后来改喜欢 B"两条旧记忆并存污染检索（P1 再加显式 supersede）。每条 fact 强制携带 `source`（chat_rule/chat_llm/event_mining）与 `evidence`（来源轮次/事件 id）——Agent Zero 的溯源约束。

### 4. 类型条件衰减（ScrubJay 思路，P0）

不同类别记忆不同寿命，检索排序用 `confidence × decay`：

| 类别 | 半衰期 | 理由 |
|---|---|---|
| profile | 30 天 | 名字/身份几乎不变 |
| preference | 14 天 | 偏好会缓慢迁移 |
| project | 7 天 | 项目状态会变化 |
| entity | 3 天 | "记住 X"多为临时上下文 |
| 情节事件 | 48 小时 | 屏幕活动时效性强 |

### 5. 反思固化（Generative Agents + 睡眠门控）

- **v1 规则提取**：对话里显式模式（"我叫X"/"记住X"/"我喜欢X"/"我在做X"）→ facts；事件层高频主题（同 task_tag 累计时长 > 30min）→ project fact。
- **睡眠门控**：固化线程只在 agent 空闲（idle 且超过阈值无交互）时运行——对齐 Google/Cornell Sleep 范式与 Sleep-Gated 披露，固化永不与交互抢资源。
- **P1 LLM 提取**：空闲期调 chat 模型抽取候选事实 → 规则校验后入库（LLM 只做候选生成，写入仍走对账器——这是和 Mem0 的关键差异：**模型提议、规则裁决**，防止幻觉直接写进记忆）。

### 5. 上下文装配

system prompt = 三段式，总预算默认 1200 token（字符估算）：
1. 语义层：confidence 排序的 facts top-M（默认 8）
2. 情节层：与当前对话相关（三维打分）的 episodes top-K（默认 5）
3. 工作层：最近 2-3 轮对话摘要

## 四、路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 分层 schema + 三维检索 + 会话持久化 + 规则固化 v1 + 装配器 + **类型条件衰减 + 溯源 evidence + 睡眠门控固化** | ✅ 本次落地 |
| P1 | embedding 向量检索（SiliconFlow bge-m3 免费通道，idle 期回填向量）✅；LLM 候选提取（梦境门控内运行）、MemOS 式反思回填调 importance、fact 显式 supersede | 进行中 |
| P2 | 日报反思回路（日报 → 候选事实 → 对账入库）、**梦境重组**（跨域配对找连接）、proactive 触发（记忆驱动的主动提醒 + 前瞻记忆意图表）、时间有效性窗口 | 待做 |

## 五、面试叙事口径

> 独立设计并实现桌面 agent 三层记忆系统：情节层（屏幕活动事件流 + 重要性启发式打分）、语义层（画像/偏好/项目事实 + 新旧对账防陈旧污染）、工作层（会话持久化可恢复）；检索采用 recency × importance × relevance 三维打分（Generative Agents 公式离线化），上下文装配 token 预算感知；写入侧模型只提议、规则裁决，杜绝幻觉直写记忆。对标 Letta 分层、Mem0 对账、Zep 时间意识、GA 反思固化各取单点，Offline 最小完备集。
