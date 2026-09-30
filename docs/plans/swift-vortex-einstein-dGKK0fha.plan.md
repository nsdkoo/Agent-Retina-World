# 桌面操控 Agent Harness 重写计划

## 背景与目标

用户反馈"打开qq都打开不了"。现状：工具层只有 10 个硬编码 handler（executor.py），系统出口仅 `webbrowser.open` + `subprocess.Popen`；剪贴板/文件/音量/窗口/GUI 操控全空白。失败根因：QQ NT 不注册 App Paths 不在 PATH，而 `_resolve_app` 只有这四级。

**调研定案**（微软 UFO²/UFO³、Agent S3、OpenAdapt、CUA）：生产级桌面 agent 的共同点是①混合策略（API/系统调用优先于 GUI 模拟）②分层动作注册表+危险分级 ③lnk/UWP 全量启动解析。

**用户拍板**：全量含 GUI 操控；纯 ctypes/标准库（powershell 单行命令允许）；意图层扩正则+宽松匹配、参数解析下沉工具层。

## 关键文件

| 文件 | 动作 |
|---|---|
| `src/screen_agent/tools/`（新包） | 新建，见下 |
| `src/screen_agent/voice/executor.py` | run() 接 registry 分发；旧 10 handler 转调 registry（薄封装，保 50 测试不破） |
| `src/screen_agent/voice/intents.py` | Intent 加 `tool`/`params` 字段；新增 9 类意图正则 |
| `config.yaml` | 加 `tools.gui_agent.max_steps/confirm`、`voice.confirm_dangerous: false` |
| `tests/test_tools_*.py`（6 个新文件） | registry/apps/files/input_clipboard/volume/gui_agent 分层 mock |

## 新包结构 `src/screen_agent/tools/`

```
registry.py    # ToolSpec/RiskLevel(SAFE/LOW/HIGH)/ToolRegistry（注册分发+HIGH确认钩子+异常兜底）——纯逻辑
_win.py        # ctypes 裸 API：SendInput 结构体/EnumWindows/剪贴板 API；非 win32 raise PlatformError
apps.py        # AppResolver 六级解析 + 索引缓存（data/cache/app_index.json，7天过期）
clipboard.py   # get/set 文本 + 截图入剪贴板（win32 流程）
files.py       # open_path/list_dir/find_files（Downloads/Desktop/Documents 深度3模糊搜索 top10）
windows.py     # list_windows（EnumWindows+进程名，复用 capture/context.py 模式）/focus_window（AttachThreadInput 双保险）
input.py       # mouse_move/click/type_text(KEYEVENTF_UNICODE 中文)/press_key/scroll（SendInput，坐标归一化 65535）
volume.py      # get/up/down/mute（VK_VOLUME 按键模拟）
system.py      # lock_screen/screenshot_to_clipboard/open_url（webbrowser 迁入）
gui_agent.py   # P1：截图→VLM 动作 JSON→SendInput→复验截图 闭环（max_steps 上限）
```

## ToolRegistry 接口（核心）

`ToolSpec(name, description, handler, risk: RiskLevel, params_doc)`；`registry.run(name, **params) -> ActionResult`：HIGH 级先走 `confirm_fn` 钩子（可注入 TTS 问答），`PlatformError` → "此功能仅支持 Windows"，其余异常统一兜底。executor 构造加可选 `registry` 参数（None 时内部 build_default_registry），旧 10 意图 handler 改薄封装转调 registry——旧测试路径不变。

## 分级实施（每步独立提交、全绿可回滚）

### P0-A 应用启动修复（最优先）
`AppResolver.resolve` 解析链：config 别名 → **开始菜单 .lnk 模糊匹配**（ProgramData + %APPDATA% 两个 Programs 目录收集 .lnk 文件名，difflib+子串双打分取 top-3，一次 powershell 批量解析 TargetPath）→ App Paths 注册表（迁入现有逻辑）→ PATH → UWP（Get-StartApps 缓存索引 → `explorer.exe shell:AppsFolder\<AppID>`）。解析结果缓存 7 天。lnk 命中直接 `os.startfile`。
**验收：真机"打开qq/微信/网易云音乐"三条全通 + 旧测试全绿。**

### P0-B 基础工具集（9 步，每步有 mock 单测）
registry 基座 → _win 结构体（sizeof 断言）→ 剪贴板（OpenClipboard 重试3次/GlobalAlloc/CF_UNICODETEXT）→ app.open/close/list（taskkill 前校验非系统进程白名单）→ 文件三件套 → 窗口两件套（AttachThreadInput 规避前台锁）→ 输入五原语（中文 KEYEVENTF_UNICODE 逐字符、滚轮 ±120）→ 音量/锁屏/截屏剪贴板 → executor 接 registry。

### P1 GUI Agent Loop
`run_gui_task(instruction, max_steps=5)`：截图（复用 ScreenCapturer）→ VLM（复用 vlm.py httpx+JSON 解析模式，新 prompt：声明截图分辨率、动作枚举 click/type/key/scroll/done/fail、坐标用原始像素、"API 优先于 GUI"）→ registry.run 执行（click/type/key 为 HIGH 确认级）→ 复验截图循环。新意图 GUI_TASK："帮我(点|点击|输入|按|选)…"。非法动作拒绝并重问一次；VLM 不可用明确报错不乱点。

### P2 意图正则扩展清单（插在 END_SESSION 之后、"打开"分支之前）
关闭X→app.close｜音量大小/多少→volume｜静音→mute｜剪贴板内容/复制一下→clip｜找文件→files.find｜列出窗口→win.list｜切到X→win.focus｜锁屏→sys.lock｜帮我点/输入→gui.task。"帮我"递归分支保持在最后。

## 测试策略
- registry/intents 纯逻辑全平台跑；apps mock subprocess.run+winreg+Path.rglob；input/clipboard mock ctypes.windll（断言 SendInput 次数与 flags）；真机用例 `skipUnless(win32)`
- 每步提交跑 `python -m unittest discover tests`（存量 50 个不许破）
- 真机冒烟清单：打开qq/微信/网易云、"复制hello"粘贴验证、音量±、列出窗口、锁屏、截屏进剪贴板、（P1）"帮我点记事本关闭X"

## 风险与规避（要点）
- powershell 冷启动 1-3s：只解析 top-3 候选 + 缓存 + 先播"正在找 xx"
- Get-StartApps 编码差异：encoding="utf-8", errors="replace"，失败降级跳过 UWP 级
- SendInput 误点：HIGH 确认 + max_steps + 复验不匹配即停（不重试乱点）；首版仅主屏
- taskkill 误杀：explorer/winlogon 等系统进程白名单硬拒绝
- 剪贴板被占：重试 3 次后退化为明确报错

## 提交切分
P0-A 应用解析修复 → P0-B1 registry+_win 基座 → P0-B2 剪贴板/文件/窗口 → P0-B3 输入/音量/系统+executor 接入 → P1 GUI loop → P2 意图全量。每个提交独立全绿。
