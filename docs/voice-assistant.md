# 语音常驻助手

## 交互方式

**不需要打开对话窗口、不需要打字。**

1. 运行 `python main.py voice`
2. 桌面右侧出现常驻侧边栏
3. 直接对着麦克风说：**「小光，截图」** / **「Retina，打开百度」** / **「小光，分析屏幕」**
4. Agent 语音播报执行结果

## 唤醒词

在 `config.yaml` 配置：

```yaml
voice:
  wake_names: ["Retina", "小光", "光光"]
```

必须包含唤醒词，后面跟指令。

## 支持的语音指令

| 说法示例 | 动作 |
| --- | --- |
| 截图 / 截屏 / 截个图 | 截图 + 屏幕理解 |
| 分析屏幕 / 看看我在干什么 | 同上 |
| 打开百度 / 打开 GitHub | 打开网页 |
| 打开 https://... | 打开指定 URL |
| 打开微信 / 打开 Cursor | 启动应用 |
| 今日总结 / 日报 | 生成每日报告 |
| 时间线 | 播报最近活动 |
| 打开面板 | 打开 Web 时间线 UI |

可在 `voice.apps` 和 `voice.urls` 里自定义别名。

## 启动方式

```powershell
# 带侧边栏（推荐）
python main.py voice

# 纯后台，无界面
python main.py voice --no-ui
```

## 依赖

需要麦克风。首次使用请安装：

```powershell
pip install SpeechRecognition pyttsx3
pip install pyaudio
```

Windows 若 `pyaudio` 安装失败，可尝试：

```powershell
pip install pipwin
pipwin install pyaudio
```

语音识别默认使用 Google 在线 STT（`zh-CN`），需联网。

## 与屏幕感知的关系

- **voice**：你主动说话 → 立即执行（截图、开网页、开应用）
- **watch**：后台定时感知 → 自动记录活动时间线

两者可同时运行：一个终端 `watch`，一个终端 `voice`。
