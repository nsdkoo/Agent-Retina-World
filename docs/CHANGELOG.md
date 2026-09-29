# 变更日志

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。  
详细过程见 [development-journal.md](development-journal.md)。

## [0.6.0] - 2026-06-25

### 新增

- Chat 客户端 `understand/chat.py`（codexzh OpenAI 兼容）
- 语音自由对话：`IntentType.CHAT`，命令优先
- CLI 子命令 `demo`（配置检查 + 启动 voice）
- 文档 `demo-quickstart.md`、`development-journal.md`、`plans/`

### 变更

- 悬浮球启动自动展开日志；TTS 上限 200 字
- 默认对话模型 gpt-5.4-mini，回退 gpt-5.4

## [0.5.0] - 2026-06-23

### 新增

- Vosk 离线中文 STT
- 60 秒免唤醒连续对话会话模式
- 可拖动悬浮球 UI（默认）

## [0.4.0] - 2026-06-23

### 新增

- 语音唤醒常驻助手
- 规则意图与 CommandExecutor
- 桌面侧边栏 UI

## [0.3.0] - 2026-06-21

### 新增

- OpenAI 兼容 VLM + 失败降级
- L3 embedding 语义去重
- Web 时间线 UI（FastAPI）

## [0.2.0] - 2026-06-19

### 新增

- L2 直方图去重
- Windows 前台窗口感知
- `timeline` / `stats` CLI
- 核心单元测试

## [0.1.0] - 2026-06-18

### 新增

- 项目初始化：截图、pHash、VLM、Activity、SQLite、主动服务
- CLI：`once` / `watch` / `report`
- 架构文档与架构图
## [0.8.0] - 2026-09-29

### 新增

- 三层记忆系统 `memory/`：MemoryStoreV2（events+importance / facts+evidence / sessions+turns）
- HybridRetriever：recency × importance × relevance 三维打分（GA 公式）
- ContextAssembler：记忆 → system prompt 三段式装配，token 预算
- Consolidator：规则固化（对话模式 + 事件高频挖掘）+ LLM 候选 hook
- 类型条件衰减（ScrubJay）：按 category 半衰期；睡眠门控固化线程（idle>5min 才跑）
- 对话流式上屏（SSE 增量 + 打字机兜底）；用户消息右对齐
- 记忆测试 14 个，全仓 39 个全绿

## [0.7.0] - 2026-09-28

### 新增

- sherpa-onnx 流式 ASR（zipformer 双语 int8），边说边出字，endpoint 自动断句
- KWS 关键词唤醒（wenetspeech zipformer），唤醒词 pypinyin 自动生成 keywords.txt
- vits 中文女声 TTS（vits-zh-hf-fanchen-C），独立播报线程 + 播报期屏蔽麦克风防回环
- AudioLoop 状态机（sounddevice 常驻流式采集）：KWS 待机 → 流式识别 → 线程池执行命令
- 「唤醒词 + 命令」一句话直达（3 秒音频环形缓冲回放）
- PyQt6 悬浮球：视网膜玻璃球 + 音量呼吸环 + 对话面板（气泡流/文字输入/屏幕活动卡片）+ 系统托盘 + 开机自启
- Chat 多后端 `chat.backends`：本地 vLLM（千问系列）→ DeepSeek API 按序回退
- 音频文件调试源 `voice.sherpa.audio_source`（无麦克风环境可验证全链路）
- 测试：多后端回退 / keywords 生成 / 冒烟脚本 / E2E 音频链路脚本

### 变更

- `voice.ui` 默认 `qt`；`--ui ball` 保留 legacy Tkinter 球
- Chat 后端失败自动切换；旧单后端配置兼容

### 移除

- Vosk / SpeechRecognition / pyttsx3 / Tkinter 侧边栏
