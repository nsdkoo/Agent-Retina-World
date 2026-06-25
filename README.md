# Agent-Retina-World

**桌面屏幕世界感知 Agent** — 让 AI 理解你在电脑上做了什么，并主动提供服务。

> 个人原创项目 · v0.6

![系统架构](docs/images/architecture.png)

## 文档

| 文档 | 说明 |
| --- | --- |
| [开发过程记录](docs/development-journal.md) | **版本演进、Git 时间线、简历叙事** |
| [变更日志](docs/CHANGELOG.md) | 按版本摘要 |
| [Plan 归档](docs/plans/README.md) | Cursor Plan 模式文档入库说明 |
| [架构设计](docs/architecture.md) | 系统设计 |
| [语音助手](docs/voice-assistant.md) | 唤醒、会话、指令 |
| [Demo 试跑](docs/demo-quickstart.md) | 5 步本地验证 |

## v0.6 更新 · LLM 对话 Demo

```powershell
python main.py demo                    # 配置检查 + 悬浮球
python main.py voice --download-model  # 首次下载离线语音模型
```

- **自由对话**：未匹配命令的语句走 codexzh Chat（gpt-5.4-mini）
- **命令优先**：截图、打开应用等仍走规则意图，节省模型成本
- 详见 [docs/demo-quickstart.md](docs/demo-quickstart.md)

## v0.5 更新 · 免唤醒 + 离线 + 悬浮球

```powershell
python main.py voice --download-model   # 离线模型（一次）
python main.py voice                    # 默认悬浮球 UI
```

- **免唤醒连续对话**：唤醒一次，60 秒内直接说指令
- **离线语音识别**：Vosk 中文模型，无网可用（`stt_engine: auto`）
- **悬浮球 UI**：52px 可拖动，双击看日志

## v0.4 更新 · 语音常驻助手

**不用打字、不用对话窗口** — 呼唤名字直接说话：

```powershell
python main.py voice
```

说 **「小光，截图」** · **「Retina，打开百度」** · **「小光，分析屏幕」**

桌面右侧常驻侧边栏，麦克风聆听 + 语音播报反馈。详见 [docs/voice-assistant.md](docs/voice-assistant.md)

## v0.3 更新

- **真实 VLM 接入**：OpenAI 兼容多模态 API，Pydantic 校验 JSON，失败自动降级启发式
- **L3 embedding 语义去重**：基于窗口指纹 / 摘要向量的余弦相似度过滤
- **Web 时间线 UI**：`python main.py serve` 启动可视化面板（活动事件 + 时间分布）

## v0.2 更新

- **L2 直方图去重**：在 pHash 之后增加缩略图直方图相似度过滤
- **前台窗口感知**：Windows 下读取活动窗口标题与进程名，启发式理解无需 VLM
- **活动聚合优化**：同场景连续帧合并为单条事件，附带 `frame_count`
- **新命令**：`timeline` 活动时间线 · `stats` 运行统计
- **单元测试**：`python -m unittest discover -s tests`

## 项目亮点

1. **降本**：多阶段截图去重（感知哈希 + 语义相似度预留），显著减少 VLM 无效调用
2. **理解**：VLM 结构化输出 — 页面分类、文本块、实体、用户行为、高价值事件
3. **记忆**：单帧语义 → 连续 Activity 时间线，SQLite 任务/事件双记忆
4. **主动服务**：基于活动知识库生成每日总结、待办推断、时间分布统计

## 系统架构

```mermaid
flowchart LR
    subgraph 感知层
        A[定时截图] --> B[感知哈希去重]
        B -->|新帧| C[VLM 页面理解]
        B -->|重复帧| X[跳过推理]
    end

    subgraph 认知层
        C --> D[页面分类]
        C --> E[实体/行为识别]
        C --> F[高价值事件抽取]
        D & E & F --> G[Activity 聚合]
    end

    subgraph 记忆层
        G --> H[(事件记忆)]
        G --> I[(任务记忆)]
    end

    subgraph 服务层
        H & I --> J[每日总结]
        H & I --> K[待办推断]
        H & I --> L[时间统计]
    end
```

## 数据流

```mermaid
sequenceDiagram
    participant S as ScreenCapturer
    participant D as Deduper
    participant V as VLM Analyzer
    participant A as ActivityAggregator
    participant M as MemoryStore
    participant P as ProactiveService

    loop 每 N 秒
        S->>S: 截取屏幕
        S->>D: 新截图
        alt 感知哈希判定重复
            D-->>S: 跳过
        else 新内容
            D->>V: 送入 VLM
            V->>V: 结构化 JSON
            V->>A: PageUnderstanding
            A->>A: 时间窗口聚合
            A->>M: 持久化 ActivityEvent
        end
    end
    P->>M: 读取活动记录
    P->>P: 生成日报/待办
```

## 模块说明

| 模块 | 路径 | 职责 |
| --- | --- | --- |
| 截图采集 | `capture/screen.py` | 多显示器截图，按时间戳归档 |
| 去重 | `dedup/hasher.py` | L1 pHash + L2 直方图相似度 |
| 页面理解 | `understand/vlm.py` | 启发式 / OpenAI 兼容 VLM |
| LLM 对话 | `understand/chat.py` | OpenAI 兼容 Chat（codexzh） |
| 前台上下文 | `capture/context.py` | Windows 活动窗口标题与进程 |
| 活动聚合 | `activity/store.py` | 时间序列事件构建与 SQLite 存储 |
| 主动服务 | `proactive/service.py` | 日报、待办、时间分布 |
| 语音助手 | `voice/` | STT、意图、悬浮球 UI |
| 编排 | `pipeline.py` | 全链路调度与统计 |

## 快速开始

```powershell
cd D:\Agent-Retina
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python main.py once          # 采集并理解一帧
python main.py watch -i 30   # 定时监听
python main.py report        # 生成每日总结
python main.py timeline      # 查看活动时间线
python main.py stats         # 运行与去重统计
python main.py serve         # Web 时间线 UI
python main.py voice         # 悬浮球语音助手（默认）
python main.py demo          # 最小 Demo（配置检查 + voice）
python main.py voice --ui sidebar
python main.py voice --download-model
python -m unittest discover -s tests
```

## 配置

复制 `config.example.yaml` 为 `config.yaml`。

### 启发式模式（默认）

`vlm.provider: heuristic` — 基于前台窗口，无需 API。

### 真实 VLM

```yaml
vlm:
  provider: openai_compatible
  base_url: https://your-api/v1
  model: qwen2.5-vl-7b-instruct
  api_key_env: VLM_API_KEY      # 推荐用环境变量
  fallback_heuristic: true      # API 失败时降级
```

### Embedding 语义去重

```yaml
embedding:
  enabled: true
  base_url: https://your-api/v1
  model: text-embedding-3-small
  api_key_env: EMBEDDING_API_KEY
  threshold: 0.92
```

## 路线图

- [x] L2 直方图二级去重
- [x] Windows 前台窗口感知
- [x] 活动时间线 CLI
- [x] embedding 语义去重
- [x] VLM 真实推理（OpenAI 兼容）
- [x] Web 时间线 UI
- [x] 语音唤醒常驻助手（呼唤名字操作）
- [x] LLM 自由对话（codexzh gpt-5.4-mini）
- [ ] 跨会话任务归并与证据追溯

## License

MIT
