# Agent-Retina-World 开发过程记录

> **用途**：项目演进全过程存档，便于写简历、面试复盘、对照 Git 历史。  
> **维护**：每完成一个 Plan 或版本里程碑，更新本文 + `docs/plans/` 归档。  
> **仓库**：https://github.com/nsdkoo/Agent-Retina-World  
> **本地路径**：`D:\Agent-Retina`

---

## 1. 项目定位（对外口径）

**Agent-Retina-World** — 个人原创的桌面**屏幕世界感知 Agent**。

核心链路：

```
感知（截图）→ 去重 → VLM 理解 → Activity 聚合 → SQLite 记忆 → 主动服务
                                    ↓
                          语音唤醒 + 悬浮球 + LLM 对话（v0.6）
```

设计动机：桌面是开发者最高频界面，通用 Agent 缺少对屏幕上下文的**持续、低侵入**感知；本项目用多阶段去重降本，用结构化 VLM 输出建时间线，再叠加语音操控与对话。

---

## 2. 版本演进总览

| 版本 | 日期 | 主题 | 关键能力 |
| --- | --- | --- | --- |
| v0.1 | 2026-06-18 | 初始化 | 截图、pHash 去重、启发式/VLM 理解、Activity、SQLite、日报 |
| v0.2 | 2026-06-19 | 感知增强 | L2 直方图去重、Windows 前台窗口、timeline/stats CLI、单元测试 |
| v0.3 | 2026-06-21 | 认知升级 | 真实 VLM API、L3 embedding 语义去重、Web 时间线 UI |
| v0.4 | 2026-06-23 | 语音助手 | 唤醒词、规则意图、侧边栏 UI、TTS 反馈 |
| v0.5 | 2026-06-23 | 体验升级 | Vosk 离线 STT、60s 免唤醒会话、可拖动悬浮球 |
| v0.6 | 2026-06-25 | 最小 Demo | codexzh 对话、gpt-5.4-mini、`python main.py demo` |

---

## 3. Git 提交时间线（完整记录）

以下与 `git log --oneline` 一致，**每条 commit 即一次可写进简历的迭代**。

```
588be2d 2026-06-25  feat: 接入 codexzh 对话，支持悬浮球最小 Demo 验证
9a1e1b4 2026-06-23  docs: 更新 v0.5 语音助手文档与测试
0f7cb56 2026-06-23  feat: 新增可拖动悬浮球 UI
43008e6 2026-06-23  feat: 支持 Vosk 离线语音识别与免唤醒连续对话
2a12811 2026-06-23  feat: 新增桌面侧边栏 UI 与语音指令文档
2cbff1a 2026-06-23  feat: 新增语音唤醒常驻助手
e9b7e92 2026-06-21  docs: 更新 v0.3 文档与语义去重单元测试
af521c6 2026-06-21  feat: 新增 Web 时间线可视化面板
f6f7ca9 2026-06-21  feat: 完善真实 VLM 接入与失败降级
87db312 2026-06-21  feat: 新增 L3 embedding 语义去重
f39b007 2026-06-19  test: 补充核心模块单元测试并更新 v0.2 文档
4a67303 2026-06-19  feat: 新增 timeline 与 stats 子命令
14b6ec9 2026-06-19  feat: 优化活动聚合与主动服务输出
9865a6b 2026-06-19  feat: 基于 Windows 前台窗口的启发式页面理解
8d9f14c 2026-06-19  feat: 新增 L2 直方图去重与运行统计
a0dd36b 2026-06-18  feat: 初始化 Agent-Retina-World 桌面屏幕感知 Agent v0.1
```

> **说明**：早期曾有一次含敏感表述的 commit，已重写历史并统一品牌为 Agent-Retina-World；公开文档与 commit 仅描述本项目自身设计。

---

## 4. 分阶段详细过程

### 阶段 A · v0.1 骨架与全链路（2026-06-18）

**目标**：从零搭可运行的屏幕感知 Pipeline。

**做了什么：**

- 项目结构：`capture/` `dedup/` `understand/` `activity/` `proactive/` `pipeline.py`
- `ScreenCapturer`：mss 多显示器截图
- `ScreenshotDeduper`：L1 感知哈希（pHash）
- `VLMAnalyzer`：启发式 + OpenAI 兼容接口预留
- `ActivityAggregator` + `MemoryStore`：SQLite 事件/任务记忆
- `ProactiveService`：日报、待办推断
- CLI：`once` / `watch` / `report`
- 文档：`docs/architecture.md`、架构图

**技术决策：**

- 自研 Pipeline，不依赖 LangChain/LangGraph
- 默认启发式 VLM，无 API 也能跑通
- YAML 配置 + 环境变量密钥

---

### 阶段 B · v0.2 降本与可观测（2026-06-19）

**目标**：减少无效 VLM 调用，增强可查询性。

**做了什么：**

- L2 直方图余弦相似度去重（布局微变过滤）
- Windows `capture/context.py` 读前台窗口标题/进程
- 启发式理解不依赖截图 API 也能给出合理分类
- Activity 合并窗口优化，`frame_count` 统计
- 新命令：`timeline`、`stats`
- 单元测试：`tests/test_core.py` 等

**可量化表述（简历可用）：**

- 三层去重（L1 pHash + L2 直方图 + L3 预留）目标降低静态桌面场景 VLM 调用 50%+

---

### 阶段 C · v0.3 真实模型与可视化（2026-06-21）

**目标**：接真实多模态 API，并让人能看见时间线。

**做了什么：**

- `OpenAICompatibleVLMAnalyzer`：httpx + Pydantic JSON 校验
- `FallbackVLMAnalyzer`：API 失败降级启发式
- L3 `EmbeddingClient` + 语义去重阈值
- FastAPI Web UI：`python main.py serve`
- 静态页展示活动事件与时间分布

**技术栈补充：** FastAPI、uvicorn、httpx

---

### 阶段 D · v0.4–v0.5 语音常驻助手（2026-06-23）

**目标**：不用打字，呼唤名字即可操控电脑。

**Plan/需求要点：**

- 唤醒词：Retina / 小光 / 光光
- 规则意图：截图、打开 URL/App、分析屏幕、日报、时间线、统计
- UI：先侧边栏，后改为默认**悬浮球**（52px、置顶、可拖）
- 离线 STT：Vosk 中文小模型，`auto` 模式无模型时回退 Google
- **会话模式**：唤醒后 60 秒内免唤醒连续指令

**模块：**

- `voice/assistant.py` — 主循环
- `voice/intents.py` — 正则意图
- `voice/executor.py` — 命令执行
- `voice/listener.py` — STT + pyttsx3 TTS
- `voice/floating_ball.py` / `sidebar.py` — UI

**文档：** `docs/voice-assistant.md`

---

### 阶段 E · v0.6 最小对话 Demo（2026-06-25）

**Plan 归档：** [plans/最小语音对话-demo.plan.md](plans/最小语音对话-demo.plan.md)

**目标：** 悬浮球 + 说话 + **LLM 回复**，本地可验证。

**做了什么：**

- 新建 `understand/chat.py`：OpenAI 兼容 Chat，codexzh `https://api.codexzh.com/v1`
- 默认 `gpt-5.4-mini`，失败回退 `gpt-5.4`；禁止 5.5
- 密钥：`OPENAI_API_KEY` 或 `~/.codex/auth.json`（不写 Git）
- `IntentType.CHAT`：命令优先，其余走对话
- 对话历史 6 条；退出会话清空
- `python main.py demo`：配置自检 + 启动 voice
- `docs/demo-quickstart.md`

**成本控管：** max_tokens 256；仅非命令语句调 LLM；不做后台自动 Chat

---

## 5. Plan 模式与文档关系

| 类型 | 存放位置 | 是否进 Git |
| --- | --- | --- |
| Cursor Plan 原文件 | `~/.cursor/plans/*.plan.md` | 否（默认） |
| 项目 Plan 归档 | [`docs/plans/`](plans/) | **是** |
| 过程总览 | 本文 + [`plans/00-对话与迭代时间线.md`](plans/00-对话与迭代时间线.md) | **是** |
| 架构设计 | `docs/architecture.md` | 是 |
| 使用说明 | `voice-assistant.md`、`demo-quickstart.md` | 是 |
| 代码变更 | Git commit | 是 |

### Plan 归档完整列表（截至 2026-06-25）

| 文档 | 类型 |
| --- | --- |
| [plans/00-对话与迭代时间线.md](plans/00-对话与迭代时间线.md) | 总索引 |
| [plans/v0.1-屏幕感知Agent初始化.plan.md](plans/v0.1-屏幕感知Agent初始化.plan.md) | 过程重建 |
| [plans/品牌与仓库规范-2026-06-18.plan.md](plans/品牌与仓库规范-2026-06-18.plan.md) | 过程重建 |
| [plans/v0.2-感知增强.plan.md](plans/v0.2-感知增强.plan.md) | 过程重建 |
| [plans/v0.3-VLM-Embedding-WebUI.plan.md](plans/v0.3-VLM-Embedding-WebUI.plan.md) | 过程重建 |
| [plans/v0.4-语音常驻助手.plan.md](plans/v0.4-语音常驻助手.plan.md) | 过程重建 |
| [plans/v0.5-离线STT与悬浮球.plan.md](plans/v0.5-离线STT与悬浮球.plan.md) | 过程重建 |
| [plans/最小语音对话-demo.plan.md](plans/最小语音对话-demo.plan.md) | **正式 Plan** |
| [plans/文档归档-过程记录.plan.md](plans/文档归档-过程记录.plan.md) | 过程重建 |

> **说明**：v0.1–v0.5 当时未使用 Plan 模式，以上根据用户原话 + Git + 会话记录回溯整理。仅 v0.6 有 Cursor 原生 `.plan.md` 原文件。

**以后每次 Plan 定稿后：**

1. 复制 Plan 到 `docs/plans/`
2. 更新本文「版本演进」与「分阶段过程」
3. 执行功能开发 → `git commit`（中文 message）
4. 可选：同步有道云项目记忆（不存密钥）

---

## 6. 模块与目录对照（简历技术点）

| 能力 | 路径 |
| --- | --- |
| 截图采集 | `src/screen_agent/capture/screen.py` |
| 前台窗口上下文 | `src/screen_agent/capture/context.py` |
| L1/L2 去重 | `src/screen_agent/dedup/hasher.py` |
| L3 语义去重 | `src/screen_agent/dedup/semantic.py` |
| VLM 理解 | `src/screen_agent/understand/vlm.py` |
| LLM 对话 | `src/screen_agent/understand/chat.py` |
| 活动记忆 | `src/screen_agent/activity/store.py` |
| 主动服务 | `src/screen_agent/proactive/service.py` |
| 编排 | `src/screen_agent/pipeline.py` |
| Web 面板 | `src/screen_agent/web/server.py` |
| 语音助手 | `src/screen_agent/voice/` |
| CLI | `main.py` |

---

## 7. 技术栈汇总

- **语言**：Python 3.10+
- **屏幕**：mss、Pillow、imagehash
- **存储**：SQLite、PyYAML
- **模型**：OpenAI 兼容 VLM / Chat / Embedding（httpx）
- **Web**：FastAPI、uvicorn
- **语音**：Vosk、SpeechRecognition、pyttsx3
- **UI**：Tkinter 悬浮球 / 侧边栏
- **测试**：unittest（16+ cases）

---

## 8. 路线图（截至 v0.6）

- [x] L1 pHash 去重
- [x] L2 直方图去重
- [x] L3 embedding 语义去重
- [x] Windows 前台窗口感知
- [x] 真实 VLM + 降级
- [x] Web 时间线 UI
- [x] 语音唤醒 + 离线 STT + 悬浮球
- [x] LLM 自由对话（codexzh gpt-5.4-mini）
- [ ] 跨会话任务归并与证据追溯
- [ ] 后台 watch 与语音对话深度联动（按需截图注入上下文）

---

## 9. 简历可用的「过程叙事」模板

> 独立设计并实现 Agent-Retina-World 桌面屏幕感知 Agent。v0.1 完成截图—去重—VLM—Activity—SQLite 全链路；v0.2 增加 L2 直方图去重与 Windows 前台上下文，降低 VLM 调用；v0.3 接入 OpenAI 兼容多模态 API 与 embedding 语义去重，并上线 FastAPI 时间线面板；v0.4–v0.5 实现 Vosk 离线语音、唤醒与会话模式、Tkinter 悬浮球 UI；v0.6 接入 codexzh 对话模型，实现命令优先、闲聊兜底的语音交互 Demo。全程自研 Pipeline，含 16+ 单元测试与完整 docs 演进记录。

---

*最后更新：2026-06-25 · 全版本 Plan/过程文档归档完成*
