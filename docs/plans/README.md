# Plan 模式文档归档说明

## 为什么要有这个目录

Cursor **Plan 模式**生成的计划文件默认保存在本机：

```
C:\Users\<用户名>\.cursor\plans\*.plan.md
```

这些文件**不会自动进入 Git 仓库**。v0.1–v0.5 多数迭代是在 **Agent 模式**下直接做的，同样没有独立 Plan 文件。

因此约定：

1. **正式 Plan** → 从 `.cursor/plans/` 复制到本目录  
2. **Agent 模式迭代** → 按 [00-对话与迭代时间线.md](00-对话与迭代时间线.md) 回溯，写成「过程重建」plan  
3. 每归档一篇 → 更新 [development-journal.md](../development-journal.md)

---

## 完整索引

| 文件 | 类型 | 版本 | 日期 |
| --- | --- | --- | --- |
| [00-对话与迭代时间线.md](00-对话与迭代时间线.md) | 总索引 | 全版本 | — |
| [v0.1-屏幕感知Agent初始化.plan.md](v0.1-屏幕感知Agent初始化.plan.md) | 过程重建 | v0.1 | 2026-06-18 |
| [品牌与仓库规范-2026-06-18.plan.md](品牌与仓库规范-2026-06-18.plan.md) | 过程重建 | — | 2026-06-18 |
| [v0.2-感知增强.plan.md](v0.2-感知增强.plan.md) | 过程重建 | v0.2 | 2026-06-19 |
| [v0.3-VLM-Embedding-WebUI.plan.md](v0.3-VLM-Embedding-WebUI.plan.md) | 过程重建 | v0.3 | 2026-06-21 |
| [v0.4-语音常驻助手.plan.md](v0.4-语音常驻助手.plan.md) | 过程重建 | v0.4 | 2026-06-23 |
| [v0.5-离线STT与悬浮球.plan.md](v0.5-离线STT与悬浮球.plan.md) | 过程重建 | v0.5 | 2026-06-23 |
| [最小语音对话-demo.plan.md](最小语音对话-demo.plan.md) | **正式 Plan** | v0.6 | 2026-06-25 |
| [文档归档-过程记录.plan.md](文档归档-过程记录.plan.md) | 过程重建 | docs | 2026-06-25 |

---

## 归档规范

1. 文件名：`阶段简称.plan.md` 或 `v0.x-主题.plan.md`
2. 文首标注：**正式 Plan** 或 **过程重建**
3. 不写 API Key、Token、第三方敏感引用
4. 关联 Git commit hash
5. 与 `development-journal.md`、`CHANGELOG.md` 同步

---

## 本机 Cursor 原文件

| 标题 | 路径 | 仓库归档 |
| --- | --- | --- |
| 最小语音对话 Demo | `~/.cursor/plans/最小语音对话_demo_b578bbfe.plan.md` | 已归档 |
| v0.1–v0.5 | **无** | 已过程重建 |

---

## 会话记录来源

Agent-Retina-World 完整对话索引（本机）：

`C:\Users\mi\.cursor\projects\d-Agent-Retina\agent-transcripts\7107d96f-2f73-4eed-bd0f-6da436305c3e\`

**注意**：transcript 不进 Git；过程要点已提炼进上述 plan 与 journal。
