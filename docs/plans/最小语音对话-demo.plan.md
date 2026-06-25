# Plan：最小语音对话 Demo（v0.6）

> **类型**：正式 Plan（Cursor Plan 模式）  
> **归档自**：`最小语音对话_demo_b578bbfe.plan.md` · 2026-06-25

## 目标

在现有悬浮球 + 语音助手基础上，实现**最小可跑 Demo**：

- 桌面悬浮球常驻
- 唤醒后可**自由对话**（不仅是固定指令）
- 接入 codexzh，默认 **gpt-5.4-mini**，失败回退 **gpt-5.4**
- **不使用 gpt-5.5**（成本考虑）

## 现状（Plan 制定时）

**已有：**

- 悬浮球 UI（52px、可拖动、双击日志）
- STT → 唤醒词 → 规则意图 → CommandExecutor
- `python main.py voice`

**缺失：**

- 未匹配指令仅返回固定提示，无 LLM 回复
- 无 `chat` 配置段、无纯文本 Chat 客户端

## 实现方案摘要

### 1. Chat 客户端

新建 `src/screen_agent/understand/chat.py`：

- OpenAI 兼容 `POST /chat/completions`
- 密钥：`OPENAI_API_KEY` → `~/.codex/auth.json`
- 模型回退：gpt-5.4-mini → gpt-5.4

### 2. 配置

`config.example.yaml` 增加 `chat:` 段（base_url、model、max_tokens、max_history）

### 3. 语音路由

- 新增 `IntentType.CHAT`
- 固定指令优先；未匹配 → CHAT
- Executor 维护对话历史（默认 6 条）

### 4. UI

- 悬浮球启动后自动展开日志
- TTS 上限 120 → 200 字

### 5. CLI

- `python main.py demo`：配置检查 + 启动 voice

### 6. 文档与测试

- `docs/demo-quickstart.md`
- 扩展 `tests/test_voice_intents.py`

## 明确不做（控成本）

- 后台自动每 30s 截屏 + Chat
- Web 聊天页
- 有道云自动拉密钥

## 验收清单

1. `python main.py demo` 启动
2. 「小光，你好」→ 有 LLM 回复 + TTS
3. 60 秒内「讲个笑话」免唤醒
4. 「截图」仍走命令，不调 Chat

## 执行结果

- Commit：`588be2d` — feat: 接入 codexzh 对话，支持悬浮球最小 Demo 验证
- 测试：16 passed
- Push：当时网络失败，需本地 `git push`
