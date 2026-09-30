# 变更日志

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。  
详细过程见 [development-journal.md](development-journal.md)。

## [0.9.0] - 2026-09-30

### 新增

- **文件操作工具集**：新建文件夹 / 移动 / 复制 / 重命名 / 归档 / 撤销，补齐此前只有只读（打开 · 列目录 · 找文件）的缺口
  - 白名单：写操作只允许落在桌面 / 下载 / 文档 / 图片 / 视频 / 音乐
  - 删除一律走回收站（SHFileOperation + FOF_ALLOWUNDO），不提供永久删除
  - 每次写操作记 `data/cache/fs_journal.json`，说「撤销」整体回滚；重名自动加 `_1` 不覆盖
- **目录归档三阶段**：先问怎么整理（按类型 / 按修改时间）→ 出「源 → 目标」清单 → 确认后才移动。子目录、系统文件与快捷方式一律跳过
- **交互式选项**：`ActionResult.options` + `ask.options` 工具，需要用户拍板时在输入框上方渲染可点按钮，点选即作为下一条指令回传
- 新意图：整理 / 归类、新建文件夹、移动、复制、重命名、删除、撤销

### 修复

- **对话工具轮被吞**：`_chat` 里 registry 是惰性初始化的，却用 `self._registry is not None` 判断，首次对话永远拿不到工具列表——模型只能说「正在为您整理桌面文件，请稍等」这类空话
- **复合指令被整句当应用名**：`打开\s*(.+)` 贪婪匹配把「打开qq给我的小号 发个消息」整句交给 AppResolver，现在只取前半段应用名
- System Prompt 补入防空话约束：没有实际调用工具就不许声称已经执行

### 测试

- 新增 `tests/test_tools_files.py`（23 例）：白名单 / 重名不覆盖 / 归档三阶段 / 撤销 / 意图路由 / 对话工具轮，全程在临时目录里跑

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
