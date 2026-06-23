# 语音常驻助手

## 交互方式

**不用打字、不用对话窗口。**

```powershell
# 1. 下载离线语音模型（约 42MB，只需一次）
python main.py voice --download-model

# 2. 启动悬浮球（默认）
python main.py voice
```

## 三种 UI

| 模式 | 命令 |
| --- | --- |
| **悬浮球**（默认） | `python main.py voice` |
| 侧边栏 | `python main.py voice --ui sidebar` |
| 无界面 | `python main.py voice --no-ui` |

悬浮球：52px 小圆球，可拖动，双击展开日志，右键退出。

## 免唤醒连续对话

1. 先说唤醒词：**「小光，截图」**
2. 进入**连续对话模式**（悬浮球变紫色）
3. 接下来 **60 秒内** 直接说指令，无需再喊名字：
   - 「打开百度」
   - 「再看看屏幕」
   - 「今日总结」
4. 说 **「退出」** / **「没事了」** 结束，或超时自动退出

配置：

```yaml
voice:
  session_mode: true
  session_duration_seconds: 60
```

## 离线语音识别

默认 `stt_engine: auto` — 有 Vosk 模型则**完全离线**，否则回退 Google 在线。

```yaml
voice:
  stt_engine: vosk    # 强制离线
  vosk_model_path: models/vosk-model-small-cn-0.22
```

下载模型：

```powershell
python main.py voice --download-model
pip install vosk pyaudio SpeechRecognition pyttsx3
```

## 支持的语音指令

| 说法 | 动作 |
| --- | --- |
| 截图 / 截屏 | 截图 + 理解 |
| 打开百度 / 打开 Cursor | 开网页 / 开应用 |
| 分析屏幕 | 屏幕理解 |
| 今日总结 | 日报 |
| 退出 / 没事了 | 结束连续对话 |

## 与屏幕感知配合

```powershell
# 终端 1：后台感知
python main.py watch -i 30

# 终端 2：语音操控
python main.py voice
```
