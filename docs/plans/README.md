# Plan 归档与开发日志说明

## 现在的机制（2026-09-30 起 · 自动）

以前这里靠一条**人工约定**：「正式 Plan 从 `~/.cursor/plans/` 复制过来」。

**靠记性的事必然断** —— 最后一次归档是 2026-06-25。之后 7、8 两个月没有提交（没开发），
而 9 月的 **77 个提交一份过程记录都没留下**。

现在换成两条自动机制：

### 1 · Plan 自动归档

`scripts/archive_plans.py` 扫各 AI 工具的 Plan 模式产物目录
（`~/.workbuddy/plans/`、`~/.cursor/plans/`、`~/.claude/plans/`、`~/.codebuddy/plans/`），
**按内容认领属于本项目的**，复制进本目录。

```bash
python scripts/archive_plans.py --list          # 看各工具目录现状
python scripts/archive_plans.py --dry-run       # 看会收哪些，不动文件
python scripts/archive_plans.py                 # 归档（幂等，重复跑不会重复）
python scripts/archive_plans.py --install-hook  # 装 git 钩子，提交即自动归档
```

**怎么判归属**：读文件内容，看有没有出现本项目标识（目录名 / 仓库路径 / 历史名）。
实测可行 —— WorkBuddy 生成的 plan 会在正文里写到项目路径。

**已归档的记在 `.archive-registry.json`**（按内容 hash），所以重复跑是安全的。

### 2 · 开发日志

[2026-09-开发日志.md](2026-09-开发日志.md) —— **每轮实质工作后由 AI 助手追加**，
规则写在项目根的 [`AGENTS.md`](../../AGENTS.md) 里。

**为什么必须有它**：Agent 模式下的开发**不产生 plan 文件**，而它是主要的开发方式
（9 月 77 个提交全是这么来的）。没有开发日志，这段时间就是彻底空白 ——
Plan 归档再全也补不上。

---

## 完整索引

| 文件 | 来源 | 版本 / 主题 | 日期 |
| --- | --- | --- | --- |
| [00-对话与迭代时间线.md](00-对话与迭代时间线.md) | 总索引 | 全版本 | — |
| [2026-09-开发日志.md](2026-09-开发日志.md) | **开发日志** | 09 月（含补记） | 2026-09 |
| [quantum-pulse-babbage-1KjXp3Ie.plan.md](quantum-pulse-babbage-1KjXp3Ie.plan.md) | **自动归档** | 评测 / 飞轮 / 自进化 | 2026-09-30 |
| [swift-nebula-einstein-JDhgWrK1.plan.md](swift-nebula-einstein-JDhgWrK1.plan.md) | **自动归档** | Agent harness 完善 | 2026-09-30 |
| [最小语音对话-demo.plan.md](最小语音对话-demo.plan.md) | 正式 Plan | v0.6 | 2026-06-25 |
| [文档归档-过程记录.plan.md](文档归档-过程记录.plan.md) | 过程重建 | docs | 2026-06-25 |
| [v0.5-离线STT与悬浮球.plan.md](v0.5-离线STT与悬浮球.plan.md) | 过程重建 | v0.5 | 2026-06-23 |
| [v0.4-语音常驻助手.plan.md](v0.4-语音常驻助手.plan.md) | 过程重建 | v0.4 | 2026-06-23 |
| [v0.3-VLM-Embedding-WebUI.plan.md](v0.3-VLM-Embedding-WebUI.plan.md) | 过程重建 | v0.3 | 2026-06-21 |
| [v0.2-感知增强.plan.md](v0.2-感知增强.plan.md) | 过程重建 | v0.2 | 2026-06-19 |
| [品牌与仓库规范-2026-06-18.plan.md](品牌与仓库规范-2026-06-18.plan.md) | 过程重建 | 品牌规范 | 2026-06-18 |
| [v0.1-屏幕感知Agent初始化.plan.md](v0.1-屏幕感知Agent初始化.plan.md) | 过程重建 | v0.1 | 2026-06-18 |

---

## 归档规范

1. **文件名**：历史归档用 `阶段简称.plan.md` / `v0.x-主题.plan.md`；
   自动归档**保留工具生成的原名**（便于回溯到源文件）
2. **文首标注来源**：`正式 Plan` / `过程重建` / `自动归档` / `开发日志`
3. 不写 API Key、Token、第三方敏感引用
4. 关联 Git commit hash
5. **历史归档不改** —— 那是在记录历史，当时的名字就是当时的名字

---

## 历史（2026-06 · 人工期）

> 保留这段以如实反映当时的做法，以及为什么它会断。

Cursor **Plan 模式**生成的计划文件默认保存在 `C:\Users\<用户名>\.cursor\plans\*.plan.md`，
**不会自动进入 Git 仓库**。而 v0.1–v0.5 多数迭代是在 **Agent 模式**下直接做的，
同样没有独立 Plan 文件。

当时的约定是：

1. 正式 Plan → 从 `.cursor/plans/` **手动复制**到本目录
2. Agent 模式迭代 → 按 [00-对话与迭代时间线.md](00-对话与迭代时间线.md) **回溯写成**「过程重建」plan
3. 每归档一篇 → **手动更新** [development-journal.md](../development-journal.md)

**结果**：2026-06-25 之后这套约定再没被执行过。它依赖「人记得做」，
而换机器（文档里的路径还是 `C:\Users\mi\`）、换 AI 工具之后，
每个工具的 plan 目录互相不知道对方存在，也没人再想起来搬。

**现在的做法是把这三步全部自动化** —— 见本文开头。

---

## 会话记录来源（历史）

`C:\Users\mi\.cursor\projects\d-Agent-Retina\agent-transcripts\7107d96f-2f73-4eed-bd0f-6da436305c3e\`

旧机器上的 Cursor 会话记录。transcript 不进 Git，
过程要点已提炼进上述 plan 与 development-journal。
