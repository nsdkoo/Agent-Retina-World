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
