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

1. 先说唤醒词：**「瑞塔，截图」**
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

## 自由对话（LLM）

唤醒后除了固定指令，也可以**自由聊天**。未匹配到命令的语句会走 Chat 模型回复。

```powershell
python main.py demo   # 检查配置 + 启动悬浮球
```

默认模型 **gpt-5.4-mini**（codexzh），失败回退 gpt-5.4。详见 [demo-quickstart.md](demo-quickstart.md)。

配置：

```yaml
chat:
  enabled: true
  base_url: https://api.codexzh.com/v1
  model: gpt-5.4-mini
  fallback_model: gpt-5.4
  api_key_env: OPENAI_API_KEY
  max_tokens: 256
```

示例：

- 「瑞塔，你好」→ LLM 回复 + 语音播报
- 「帮我讲个笑话」→ 连续对话（60 秒内免唤醒）

## 支持的语音指令

| 说法 | 动作 |
| --- | --- |
| 截图 / 截屏 | 截图 + 理解 |
| 打开百度 / 打开 Cursor | 开网页 / 开应用 |
| 分析屏幕 | 屏幕理解 |
| 今日总结 | 日报 |
| 退出 / 没事了 | 结束连续对话 |
| 整理桌面 / 整理下载 | 目录归档（先给选项与清单，确认后才动） |
| 新建文件夹 素材 | 桌面新建文件夹 |
| 把 a.pdf 移动到 文档 | 移动 |
| 把 a.txt 改名为 b.txt | 重命名 |
| 删除 临时.txt | 放进回收站 |
| 撤销 | 回滚上一次文件操作 |
| 读一下 报告.md | 读文件内容（带行号） |
| 在 桌面 里搜 TARGET | 按内容搜索 |
| 列出 *.py | 按通配符列路径 |
| 把 笔记.md 里的 A 改成 B | 精确替换（匹配到多处就不动） |
| 执行 dir | 跑一条命令：只读放行，破坏性要确认，致命直接拒 |

## 文件操作的安全边界

写操作只在**桌面 / 下载 / 文档 / 图片 / 视频 / 音乐**里动手，其他位置一律拒绝。

- **不覆盖**：目标同名时自动加 `_1` / `_2`
- **不永久删**：删除走系统回收站，随时可还原；目录只接受空目录，不递归删
- **可撤销**：每次写操作记 `data/cache/fs_journal.json`，说「撤销」整体回滚，归档留下的空分类目录一并清掉
- **归档三步**：说「整理桌面」→ 弹出「按类型归档 / 按修改时间归档 / 先不动」→ 选完出「源 → 目标」清单 → 确认才移动
- 归档跳过子目录、`desktop.ini` 这类系统文件，以及 `.lnk` / `.url` 快捷方式（桌面上放快捷方式本来就是正常状态）

## 需要拍板时会弹选项

Agent 不确定的事不替用户猜，把候选直接摆出来（面板输入框上方），点一下就当作你把这句话说了一遍：

- 「整理桌面文件」→ 按类型归档 / 按修改时间归档 / 先不动
- 清单出来后 → 确认归档 / 算了，先不动

语音场景同样管用：直接念选项里那句话即可。

## 多步任务（Agent 模式）

一句话里带「然后 / 接着 / 再」这类连接词的复合指令，会被拆成步骤依次执行，每步结果实时回报：

> 「瑞塔，新建文件夹 素材 然后 把 桌面/报告.pdf 移动到 素材」

面板里会依次看到：

- 拆成 2 步
- ✓ 第 1 步 · 新建文件夹 素材
- ✓ 第 2 步 · 把 桌面/报告.pdf 移动到 素材
- 做完了 2/2 步

中途需要拍板的步骤会挂起，答「继续执行 / 跳过这一步 / 取消任务」接着走。
挂起时你要是说了别的事（比如「打开百度」），任务会被停掉、那句话照常执行——
新指令不会被吞进任务里。

任务与每一步都落 `data/agent/tasks.db`，重启后能查「昨天那个活儿做到哪了」。

```yaml
agent:
  enabled: true
  permission_mode: smart   # auto 全自动 | approve 每次问 | smart 智能批准 | chat 仅对话
  max_steps: 5             # 单次任务最多拆几步
```

架构细节见 [agent-architecture.md](agent-architecture.md)。

## 与屏幕感知配合

```powershell
# 终端 1：后台感知
python main.py watch -i 30

# 终端 2：语音操控
python main.py voice
```
