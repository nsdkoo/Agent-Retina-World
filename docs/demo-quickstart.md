# 最小 Demo 快速试跑

5 步在本机验证「悬浮球 + 语音对话」。

## 1. 安装依赖

```powershell
cd D:\Agent-Retina
pip install -r requirements.txt
```

## 2. 下载离线语音模型（首次）

```powershell
python main.py voice --download-model
```

约 42MB，下载到 `models/vosk-model-small-cn-0.22/`。

## 3. 配置 API（对话模型）

默认使用 **codexzh** + **gpt-5.4-mini**（失败自动回退 gpt-5.4）。

密钥优先级：

1. 环境变量 `OPENAI_API_KEY` 或 `CHAT_API_KEY`
2. 本机 Codex 认证 `~/.codex/auth.json`（若已安装 Codex CLI 通常已有）

无需改代码，复制 `config.example.yaml` 为 `config.yaml` 即可（首次运行会自动生成）。

## 4. 启动 Demo

```powershell
python main.py demo
```

会打印配置检查结果，并启动桌面悬浮球。

等价命令：`python main.py voice`

## 5. 试说

1. 对着麦克风说：**「小光，你好」**
2. 悬浮球变绿（听）→ 变蓝（处理）→ 日志出现回复，并语音播报
3. 60 秒内直接说：**「帮我讲个笑话」**（免唤醒）
4. 说 **「截图」** → 走屏幕感知命令，不浪费对话额度
5. 说 **「退出」** → 结束连续对话

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 没反应 | 检查麦克风权限；先 `--download-model` |
| 对话失败 | 确认 API Key；看终端 `对话失败` 详情 |
| 模型不可用 | 配置 `chat.fallback_model: gpt-5.4` |
| 悬浮球被挡 | 拖动到屏幕边缘；双击展开日志 |

## 成本说明

- 默认 `gpt-5.4-mini`，`max_tokens: 256`
- 只有非命令语句才调用 LLM
- 不使用 gpt-5.5

## 项目文档

完整开发过程（v0.1→v0.6、Git 时间线、简历叙事）：[development-journal.md](development-journal.md)
