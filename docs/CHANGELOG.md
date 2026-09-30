# 变更日志

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。  
详细过程见 [development-journal.md](development-journal.md)。

## [1.2.0] - 2026-09-30

### 新增

- **桌面常驻观察**（`capture/watcher.py`）：事件驱动采集——只在「前台窗口切换」时记一笔，
  不空转、不定时截图。定时截图在你看同一个页面五分钟时会白拍十张，且全是重复帧
- **UIA 结构化读取**（`capture/uia.py`）：走 Windows 无障碍 API（读屏软件用的那套）拿屏幕上
  的真实文本，对标 Screenpipe 的 accessibility-first 路线。比「截图 → VLM 识图」便宜一到两个
  数量级，文本精确、不吃显卡。实测一次约 1.4 秒读到 200 条界面文本
- **隐私闸门**（`capture/privacy.py`）：三层防线——窗口/应用黑名单（密码管理器 / 银行 / 支付）、
  敏感内容识别（API key / 卡号 / 身份证 / 私钥）、记录时段。被拦下的事件**完全不落盘**，
  连「几点用过什么应用」的骨架都不留
- **行为日志与检索**（`memory/journal.py`）：SQLite + FTS5 全文索引。中文先做 bigram 切分——
  FTS5 默认按单字切，搜「会议」会命中所有含「会」或「议」的文本，bigram 之后才精准
- 新工具 `journal.today`（今天干了什么 + 各应用停留时长）、`journal.search`（按内容回忆）
- 新意图：「我今天干了什么」「找一下我之前看的 xxx」
- `assistant` 常驻观察线程：与语音循环同进程、互不阻塞，窗口切换才动一下
- 记录自动过期：`purge_before(days)`，常驻记录不能只涨不落

### 变更

- 工具总数 36 → 38
- `config.yaml` / `config.example.yaml` 新增 `perception` 段
  （enabled / db_path / use_uia / uia_timeout / active_hours / deny_apps）

### 修复

- **UIA 根元素取错**：原实现从 `FocusedElement` 向上找顶层窗口，焦点落在任务栏或开始菜单时
  会一路爬到任务栏，抓回来一排「开始 / 搜索 / 任务视图」。改为用前台窗口句柄
  `AutomationElement.FromHandle` 直接取根元素
- **只记前台会漏关键信息**：现在每条观察附上**同屏所有可见窗口标题**并进全文索引——
  焦点在知乎时，搜「淘保函」「WorkBuddy」也能找回那一刻

### 测试

- 新增 `scripts/live_test_perception.py`：23 项实战验证（UIA 采集 / 中文检索精度 /
  隐私闸门 / 日报统计 / 过期清理），全绿

## [1.1.0] - 2026-09-30

### 新增

- **文件内容层**（对标 Codex / WorkBuddy 的 read / write / edit / grep / glob）：
  - `files.read` 读文本内容：带行号、支持分页、512KB 上限、二进制自动识破
  - `files.write` 写文件：覆盖前自动备份到 `data/backups/`，可撤销；支持 append
  - `files.edit` 精确替换一处：零匹配或多匹配都拒绝——宁可不动，也不改错地方
  - `files.grep` 按内容搜索：支持正则与扩展名过滤
  - `files.glob` 通配符匹配路径
- **受限命令执行** `shell.run`（对标 Codex 的 `exec_command`）：
  - 三级分类：`deny` 直接拒（格式化 / 删盘根 / 关机 / 改注册表 / fork 炸弹）、
    `safe` 只读放行、`ask` 其余要确认
  - `git` 单独细分：`status` 放行，`push` / `reset --hard` / `clean -fd` 必须问
  - 工作目录限制在白名单内，超时默认 30 秒（上限 300），输出按字符截断
  - 照搬 Codex 的三条 Windows 安全规则：不跨 shell 组合破坏性命令、递归操作前验证
    解析后的绝对路径、后台进程隐藏窗口
- **额外可写根目录**：`files.set_extra_roots()` 与 `build_default_registry(extra_roots=...)`，
  可在配置里显式放开白名单之外的目录
- 意图层补齐：读文件 / 写文件 / 替换 / 内容搜索 / 通配匹配 / 列目录 / 执行命令

### 变更

- 工具总数 30 → 36

### 测试

- 新增 `tests/test_tools_shell.py`（14 例），全仓 156 例
- 新增 `scripts/live_test_file_ops.py`：39 项实战验证脚本，全程沙箱、不碰真实目录

## [1.0.0] - 2026-09-30

### 新增

- **Agent 运行时**（`src/screen_agent/agent/`）：把「单步意图 + 自由对话」升级成完整 Agent 架构
  - `events.py` 事件总线：plan / action / observation / ask / state_changed / finish，
    UI 与日志都只是订阅者，加一路观测不用碰主循环
  - `state.py` 任务状态机：9 态显式迁移表，非法跳转直接拒；带步骤记录与断点游标
  - `policy.py` 权限策略：四种模式（auto / approve / smart / chat）× 工具级
    AlwaysAllow / AskBefore / NeverAllow × 危险参数拦截
  - `planner.py` 规划器：复合句切分交给意图层试跑，切不动交 LLM 出 JSON 并强校验
    （工具已注册、参数键合法、步数上限）
  - `controller.py` 主循环：规划 → 逐步执行 → 审批挂起 → 恢复 → 汇总
  - `trajectory.py` 轨迹持久化：tasks / steps / events 三表落 SQLite，支持续跑
- **多步任务**：「新建文件夹 素材 然后 把 桌面/报告.pdf 移动到 素材」会被拆成两步依次执行
- **事件可观测**：任务每步进度推给面板（Info 小字，不语音播报）
- **Steering / Follow-up**：挂起时说别的算改变方向（任务停掉、那句话交回正常路由）；
  想「做完这件再做那件」用 `follow_up()` 排队
- **并行只读步骤**：连续安全步骤线程池并行，有副作用的一律串行

### 变更

- `files._resolve_dst` 支持短名目标：「把 X 移动到 素材」会先在源目录与桌面找同名文件夹，
  不再当成相对路径被白名单挡掉
- `config.yaml` / `config.example.yaml` 新增 `agent` 段（enabled / permission_mode / max_steps）

### 修复

- sqlite 连接未关闭：Windows 上 `with sqlite3.connect(...)` 只是事务上下文，连接不关，
  临时库文件被锁住（测试清理报 WinError 32）
- `auto_allows_high` 被危险动作检查短路，那个开关实际不生效
- 并行分组绕过打转检测：并行候选现在同样计入签名次数

### 测试

- 新增 `tests/test_agent_core.py`（37 例），全仓 142 例

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
