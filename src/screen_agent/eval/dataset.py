"""评测黄金集：分池 + 版本化 + JSONL 存取。

对标 2026 年 LLM 评测的通行做法，三条纪律先说清楚：

1. **样本来源必须是真实失败，不是编的**——编出来的样本编码的是「我们以为用户会怎么用」，
   而真实用户会打错字、一句话问两件事、贴一坨日志。所以这个文件里内置的是
   种子样本，真正撑起评测的是从 `data/` 里挖出来的真实困难样本。
2. **holdout 要比开发用的数据新**，同一个场景的近似句必须留在同一侧，否则分数是自欺欺人。
3. **数据集和 baseline 都是版本化产物**，不钉版本的话「分数涨了」毫无意义。

分三池（这套分法来自生产团队的实践）：

- **anchor** 锚点：严重历史失败 + 产品不变量。稳定不动，用来纵向对比
- **rolling** 滚动：贴近当前流量的代表样本，定期替换
- **challenge** 挑战：新能力、对抗输入、薄弱切片。**默认当 holdout 用**
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

POOLS = ("anchor", "rolling", "challenge")

# 活动分类的类别，与 understand/classify.py 的 ACTIVITY_LABELS 对齐
ACTIVITIES = ("coding", "writing", "meeting", "reading", "chatting", "browsing", "idle", "other")


@dataclass
class ActivityCase:
    """一条活动分类评测样本。"""

    case_id: str
    text: str                 # 屏幕文本（通常是 SightEvent.digest）
    expect: str               # 期望的活动类型
    window_title: str = ""
    app: str = ""
    pool: str = "rolling"
    note: str = ""

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class PrivacyCase:
    """一条隐私闸门评测样本。

    隐私用例的指标是**召回率**而不是准确率：漏放一次就是事故，
    误拦一次只是少记一条。两者的代价完全不对称。

    `channel_expectations` 表达通道级期望：`{"a11y_text": False}` 表示
    "这一条不该跳过整条记录，但正文不能留"。留空则只看 `expect_blocked` 的整体结论。
    """

    case_id: str
    title: str
    process: str
    expect_blocked: bool
    texts: list[str] = field(default_factory=list)
    pool: str = "anchor"
    note: str = ""
    # ↓ 新增字段一律放末尾：SEED_PRIVACY 是位置参数构造的，
    #   插在中间会让后面所有参数错位（这个坑刚踩过）
    ocr_text: str = ""
    channel_expectations: dict[str, bool] = field(default_factory=dict)

    def to_row(self) -> dict:
        return asdict(self)


# ---- 种子样本 ----
# 配比按通行做法：anchor 少而精（核心不变量 + 严重历史失败），
# rolling 占大头（贴近当前流量），challenge 是边界与对抗用例（默认当 holdout）。
# 真正撑起评测的是飞轮从真实使用里挖出来的样本，这些只是起步骨架。
SEED_ACTIVITY: tuple[ActivityCase, ...] = (
    # ---------- coding ----------
    ActivityCase("a01", "def analyze(self): raise ValueError index out of range", "coding",
                 "main.py - Visual Studio Code", "编辑器", "anchor", "典型报错现场"),
    ActivityCase("a02", "Traceback (most recent call last) File pytest fixtures", "coding",
                 "pytest - 终端", "终端", "rolling"),
    ActivityCase("a03", "git status On branch main nothing to commit", "coding",
                 "终端", "终端", "rolling", "只读命令也算在干活"),
    ActivityCase("a18", "Merge branch 'feature/memory' into main Conflicts resolved", "coding",
                 "终端", "终端", "rolling"),
    ActivityCase("a19", "npm run build vite v6 building for production", "coding",
                 "终端", "终端", "rolling"),
    ActivityCase("a20", "class PolicyEngine def judge self spec params Decision", "coding",
                 "policy.py - Cursor", "编辑器", "rolling"),
    ActivityCase("a21", "SELECT id, name FROM sights WHERE ts >= ? ORDER BY ts DESC", "coding",
                 "query.sql - DataGrip", "编辑器", "challenge", "写 SQL 也是写代码"),
    ActivityCase("a22", "Pull request 142 Files changed 8 Review required", "coding",
                 "GitHub - Google Chrome", "浏览器", "challenge", "浏览器里也在写代码"),

    # ---------- writing ----------
    ActivityCase("a04", "与会人员 张三 李四 结论 排期顺延一周 行动项", "writing",
                 "会议纪要.docx - WPS", "办公", "anchor", "文档≠开会，这是易混点"),
    ActivityCase("a05", "产品需求说明书 第三章 功能清单 表格", "writing",
                 "需求.md - Typora", "编辑器", "rolling"),
    ActivityCase("a23", "季度汇报 第 1 页 封面 目录 图表 动画", "writing",
                 "Q3 汇报.pptx - PowerPoint", "办公", "rolling"),
    ActivityCase("a24", "A 列 姓名 B 列 金额 C 列 合计 SUM 公式", "writing",
                 "预算表.xlsx - Excel", "办公", "rolling"),
    ActivityCase("a25", "本周完成 下周计划 风险项 需要支持", "writing",
                 "周报.md - Typora", "编辑器", "rolling", "写周报"),
    ActivityCase("a26", "甲方乙方 权利义务 违约责任 签字盖章", "writing",
                 "服务合同.docx - WPS", "办公", "rolling"),
    ActivityCase("a27", "## 架构设计 ### 事件流 ### 状态机 修订记录", "writing",
                 "architecture.md - Cursor", "编辑器", "challenge", "写文档和写代码同一个应用"),

    # ---------- meeting ----------
    ActivityCase("a06", "腾讯会议 共享屏幕中 麦克风 摄像头 离开会议", "meeting",
                 "腾讯会议", "会议", "anchor", "真开会"),
    ActivityCase("a07", "Zoom Meeting Participants Chat Raise Hand", "meeting",
                 "Zoom Meeting", "Zoom", "rolling"),
    ActivityCase("a28", "Microsoft Teams Meeting 静音 共享 参会者 9 人", "meeting",
                 "Teams Meeting", "Teams", "rolling"),
    ActivityCase("a29", "飞书会议 正在录制 参会人 举手 聊天", "meeting",
                 "飞书会议", "飞书", "rolling"),
    ActivityCase("a30", "面试官 候选人 自我介绍 项目经历 反问环节", "meeting",
                 "腾讯会议", "会议", "challenge", "面试也是会议"),
    ActivityCase("a31", "语音通话中 00:12:34 挂断 静音", "meeting",
                 "微信语音通话", "WeChat.exe", "challenge", "通话 ≠ 发消息"),

    # ---------- reading ----------
    ActivityCase("a08", "如何设计 Agent 记忆系统 向量检索 长上下文", "reading",
                 "知乎 - Microsoft Edge", "浏览器", "rolling"),
    ActivityCase("a09", "Qwen2.5-VL 技术报告 第 3 节 实验设置", "reading",
                 "arxiv.org - Chrome", "浏览器", "anchor", "看资料≠写文档"),
    ActivityCase("a32", "API 参考 认证方式 请求参数 返回示例", "reading",
                 "docs.python.org - Chrome", "浏览器", "rolling"),
    ActivityCase("a33", "知识库 产品手册 常见问题 第 4 章", "reading",
                 "内部知识库 - Chrome", "浏览器", "rolling"),
    ActivityCase("a34", "第 2 章 相关工作 图 3 表格 2 参考文献", "reading",
                 "paper.pdf - Edge", "浏览器", "rolling"),
    ActivityCase("a35", "公众号文章 阅读原文 点赞 在看 分享", "reading",
                 "公众号文章 - 微信", "WeChat.exe", "challenge", "微信里看文章"),
    ActivityCase("a36", "教程 第一步 安装 第二步 配置 第三步 运行", "reading",
                 "快速开始 - Chrome", "浏览器", "rolling"),

    # ---------- chatting ----------
    ActivityCase("a10", "张三：明天上午评审改到三点 李四：收到", "chatting",
                 "微信", "WeChat.exe", "anchor"),
    ActivityCase("a11", "群聊 项目组 未读消息 99+", "chatting",
                 "钉钉", "DingTalk.exe", "rolling"),
    ActivityCase("a37", "新消息 好友 朋友圈 通讯录 收藏", "chatting",
                 "QQ", "QQ.exe", "rolling"),
    ActivityCase("a38", "飞书 消息 群组 未读 提及我的", "chatting",
                 "飞书", "飞书", "rolling"),
    ActivityCase("a39", "Slack general channel unread messages thread", "chatting",
                 "Slack", "Slack.exe", "rolling"),
    ActivityCase("a40", "Telegram Saved Messages last seen 12:30", "chatting",
                 "Telegram", "Telegram.exe", "challenge"),
    ActivityCase("a17", "微信文件传输助手 发送了一个 报告.pdf", "chatting",
                 "文件传输助手", "WeChat.exe", "challenge", "发文件仍是聊天场景"),

    # ---------- browsing ----------
    ActivityCase("a12", "热搜榜 今日头条 推荐 视频 播放", "browsing",
                 "首页 - Microsoft Edge", "浏览器", "rolling"),
    ActivityCase("a13", "B站 番剧 追番 弹幕 播放列表", "browsing",
                 "哔哩哔哩", "浏览器", "rolling"),
    ActivityCase("a41", "微博 热搜 关注 超话 转发 评论", "browsing",
                 "微博 - Chrome", "浏览器", "rolling"),
    ActivityCase("a42", "YouTube Home Trending Subscriptions Watch later", "browsing",
                 "YouTube - Chrome", "浏览器", "rolling"),
    ActivityCase("a43", "淘宝 购物车 宝贝详情 加入购物车 立即购买", "browsing",
                 "淘宝网 - Chrome", "浏览器", "challenge", "购物算浏览"),
    ActivityCase("a44", "小红书 笔记 点赞 收藏 评论 关注", "browsing",
                 "小红书 - Chrome", "浏览器", "rolling"),

    # ---------- idle / other ----------
    ActivityCase("a14", "", "idle", "", "", "challenge", "空内容应判空闲"),
    ActivityCase("a45", "锁屏 输入密码 登录", "idle", "锁屏", "LogonUI.exe",
                 "challenge", "锁屏不是工作"),
    ActivityCase("a15", "设置 系统 显示 网络 蓝牙 声音", "other",
                 "设置", "SystemSettings.exe", "challenge", "系统设置不属于任何工作类型"),
    ActivityCase("a46", "进程 内存 CPU 磁盘 服务 性能", "other",
                 "任务管理器", "Taskmgr.exe", "challenge"),
    ActivityCase("a47", "此电脑 本地磁盘 下载 文档 图片", "other",
                 "文件资源管理器", "explorer.exe", "rolling"),
    ActivityCase("a48", "回收站 清空回收站 还原此项目", "other",
                 "回收站", "explorer.exe", "challenge"),

    # ---------- 边界：同一个应用里的两种活动 ----------
    ActivityCase("a16", "Cursor Agent 正在生成代码 diff 应用", "coding",
                 "Cursor", "编辑器", "challenge", "编辑器里看 AI 写代码"),
    ActivityCase("a49", "VS Code 扩展市场 安装插件 评价 下载量", "other",
                 "扩展 - Visual Studio Code", "编辑器", "challenge", "逛插件市场不算写代码"),
    ActivityCase("a50", "Chrome 开发者工具 Elements Console Network", "coding",
                 "DevTools - Chrome", "浏览器", "challenge", "浏览器里调试"),
    ActivityCase("a51", "Word 修订模式 批注 接受更改 拒绝更改", "writing",
                 "方案.docx - Word", "办公", "challenge"),

    # ---------- 第二批（扩到 110 条打底；真实价值仍在飞轮挖出来的样本）----------
    # coding
    ActivityCase("a52", "docker compose up 容器启动中 health check", "coding",
                 "终端", "终端", "rolling"),
    ActivityCase("a53", "TypeError: unsupported operand type(s) for +", "coding",
                 "main.py - Visual Studio Code", "编辑器", "rolling"),
    ActivityCase("a54", "git rebase -i HEAD~3 pick squash", "coding",
                 "终端", "终端", "rolling"),
    ActivityCase("a55", "def test_retry(): assert attempts == 3", "coding",
                 "test_client.py - PyCharm", "编辑器", "rolling"),
    ActivityCase("a56", "CREATE INDEX idx_facts_conf ON facts(confidence)", "coding",
                 "schema.sql - DataGrip", "编辑器", "challenge"),
    ActivityCase("a57", "kubectl get pods -n production 状态 Running", "coding",
                 "终端", "终端", "rolling"),
    ActivityCase("a58", "import pandas as pd 数据清洗 缺失值填充", "coding",
                 "analysis.ipynb - Jupyter", "浏览器", "rolling"),
    ActivityCase("a59", "Review changes 3 files changed +120 -45", "coding",
                 "GitHub - Chrome", "浏览器", "rolling"),
    ActivityCase("a60", "eslint 检测到 5 个问题 3 个可自动修复", "coding",
                 "终端", "终端", "rolling"),
    ActivityCase("a61", "pubspec.yaml 依赖 版本冲突 解决", "coding",
                 "编辑器", "编辑器", "challenge", "不常见的工具链"),
    ActivityCase("a62", "redis-cli keys * 内存占用 分析", "coding",
                 "终端", "终端", "rolling"),
    ActivityCase("a63", "typedef struct { int fd; char buf[256]; }", "coding",
                 "net.c - Visual Studio Code", "编辑器", "challenge", "C 语言"),
    # writing
    ActivityCase("a64", "投标文件 技术方案 第 4 章 实施计划", "writing",
                 "投标书.docx - WPS", "办公", "rolling"),
    ActivityCase("a65", "月报 数据汇总 同比 环比 趋势图", "writing",
                 "月报.xlsx - Excel", "办公", "rolling"),
    ActivityCase("a66", "用户调研报告 样本量 结论 建议", "writing",
                 "调研.md - Typora", "编辑器", "rolling"),
    ActivityCase("a67", "PRD 需求背景 目标用户 功能列表 优先级", "writing",
                 "PRD.md - Obsidian", "编辑器", "rolling"),
    ActivityCase("a68", "培训材料 第 1 页 目录 讲师 时间安排", "writing",
                 "培训.pptx - PowerPoint", "办公", "rolling"),
    ActivityCase("a69", "报销单 差旅费 交通 住宿 合计", "writing",
                 "报销.xlsx - Excel", "办公", "challenge"),
    ActivityCase("a70", "README 安装 使用方法 贡献指南", "writing",
                 "README.md - Visual Studio Code", "编辑器", "challenge", "写文档和写代码同一个应用"),
    ActivityCase("a71", "翻译 中英对照 术语表 校对", "writing",
                 "术语表.xlsx - Excel", "办公", "rolling"),
    # meeting
    ActivityCase("a72", "飞书会议 屏幕共享 白板 批注", "meeting",
                 "飞书会议", "飞书", "rolling"),
    ActivityCase("a73", "Google Meet 参会 12 人 正在发言", "meeting",
                 "Google Meet - Chrome", "浏览器", "rolling"),
    ActivityCase("a74", "钉钉视频会议 会议纪要 自动生成中", "meeting",
                 "钉钉会议", "钉钉", "rolling"),
    ActivityCase("a75", "周会 每个人同步进度 轮到你了", "meeting",
                 "腾讯会议", "会议", "rolling"),
    ActivityCase("a76", "客户沟通会 需求确认 报价 交付时间", "meeting",
                 "Zoom Meeting", "Zoom", "challenge"),
    ActivityCase("a77", "技术评审会 方案对比 A 方案 B 方案", "meeting",
                 "腾讯会议", "会议", "rolling"),
    # reading
    ActivityCase("a78", "掘金 前端周刊 第 12 期 阅读", "reading",
                 "掘金 - Chrome", "浏览器", "rolling"),
    ActivityCase("a79", "官方文档 快速开始 示例代码 说明", "reading",
                 "docs - Chrome", "浏览器", "rolling"),
    ActivityCase("a80", "论文 摘要 引言 方法 实验结果", "reading",
                 "paper.pdf - Edge", "浏览器", "rolling"),
    ActivityCase("a81", "rfc 规范 第 3 节 术语定义", "reading",
                 "RFC 文档 - Chrome", "浏览器", "challenge"),
    ActivityCase("a82", "SegmentFault 问答 高赞回答 评论", "reading",
                 "SegmentFault - Chrome", "浏览器", "rolling"),
    ActivityCase("a83", "stackoverflow How to fix AttributeError", "reading",
                 "Stack Overflow - Chrome", "浏览器", "rolling"),
    ActivityCase("a84", "内部 wiki 部署手册 步骤 1 2 3", "reading",
                 "wiki - Chrome", "浏览器", "rolling"),
    ActivityCase("a85", "Medium article best practices for LLM agents", "reading",
                 "Medium - Chrome", "浏览器", "rolling"),
    # chatting
    ActivityCase("a86", "工作群 通知 明天全员大会 收到请回复", "chatting",
                 "企业微信", "WeChat.exe", "rolling"),
    ActivityCase("a87", "私聊 在吗 有个事想请教", "chatting",
                 "微信", "WeChat.exe", "rolling"),
    ActivityCase("a88", "Discord general 频道 新消息 3 条", "chatting",
                 "Discord", "Discord.exe", "challenge"),
    ActivityCase("a89", "飞书 群公告 已读 12/20", "chatting",
                 "飞书", "飞书", "rolling"),
    ActivityCase("a90", "客户群 问题反馈 已回复", "chatting",
                 "企业微信", "WeChat.exe", "rolling"),
    # browsing
    ActivityCase("a91", "豆瓣 电影 评分 短评 想看", "browsing",
                 "豆瓣 - Chrome", "浏览器", "rolling"),
    ActivityCase("a92", "Steam 商店 特卖 愿望单", "browsing",
                 "Steam", "steam.exe", "challenge"),
    ActivityCase("a93", "京东 商品详情 规格 评价 加入购物车", "browsing",
                 "京东 - Chrome", "浏览器", "rolling"),
    ActivityCase("a94", "网易云音乐 每日推荐 歌单 播放", "browsing",
                 "网易云音乐", "cloudmusic.exe", "rolling"),
    ActivityCase("a95", "Twitter timeline trending topics", "browsing",
                 "Twitter - Chrome", "浏览器", "rolling"),
    ActivityCase("a96", "Reddit r/programming top posts", "browsing",
                 "Reddit - Chrome", "浏览器", "rolling"),
    # other / idle
    ActivityCase("a97", "磁盘清理 临时文件 释放空间", "other",
                 "磁盘清理", "cleanmgr.exe", "challenge"),
    ActivityCase("a98", "设备管理器 显卡 驱动 更新", "other",
                 "设备管理器", "mmc.exe", "challenge"),
    ActivityCase("a99", "计算器 标准 科学", "other",
                 "计算器", "Calculator.exe", "rolling"),
    ActivityCase("a100", "画图 画笔 橡皮擦 颜色", "other",
                 "画图", "mspaint.exe", "challenge"),
    ActivityCase("a101", "Windows 更新 正在安装 请勿关闭", "idle",
                 "Windows 更新", "SystemSettings.exe", "challenge"),
    ActivityCase("a102", "屏幕保护程序 幻灯片", "idle",
                 "屏幕保护", "scrnsave.exe", "challenge"),
    # 更多边界
    ActivityCase("a103", "B站 直播 弹幕 打赏", "browsing",
                 "哔哩哔哩直播", "浏览器", "challenge", "直播算浏览"),
    ActivityCase("a104", "腾讯文档 多人协作 光标位置 编辑中", "writing",
                 "腾讯文档 - Chrome", "浏览器", "challenge", "浏览器里写文档"),
    ActivityCase("a105", "Figma 设计稿 图层 组件 导出", "other",
                 "Figma - Chrome", "浏览器", "challenge", "设计工具不在八类里"),
    ActivityCase("a106", "Postman GET /api/users 200 OK", "coding",
                 "Postman", "Postman.exe", "challenge", "接口调试"),
    ActivityCase("a107", "VS Code 调试控制台 断点 变量监视", "coding",
                 "调试 - Visual Studio Code", "编辑器", "rolling"),
    ActivityCase("a108", "钉钉 考勤打卡 上班打卡成功", "other",
                 "钉钉", "DingTalk.exe", "challenge", "打卡不是工作内容"),
    ActivityCase("a109", "邮箱 收件箱 未读 5 标记为已读", "chatting",
                 "Outlook", "OUTLOOK.EXE", "challenge", "邮件归到沟通"),
    ActivityCase("a110", "日历 今日安排 会议提醒", "other",
                 "日历", "outlook.exe", "challenge"),
)

SEED_PRIVACY: tuple[PrivacyCase, ...] = (
    PrivacyCase("p01", "1Password - 保险库", "1password.exe", True, [], "anchor", "密码管理器"),
    PrivacyCase("p02", "中国建设银行 - 个人网银", "chrome.exe", True, [], "anchor", "银行"),
    PrivacyCase("p03", "支付宝 - 我的", "chrome.exe", True, [], "anchor"),
    PrivacyCase("p04", "记事本", "notepad.exe", True, ["sk-abcdef1234567890abcdef1234567890"],
                "anchor", "正文含 API key"),
    PrivacyCase("p05", "订单详情", "chrome.exe", True, ["6222 0212 3456 7890"],
                "anchor", "卡号"),
    PrivacyCase("p06", "身份证复印件", "wps.exe", True, ["11010119900307123X"],
                "challenge", "身份证号"),
    PrivacyCase("p07", "main.py - Visual Studio Code", "编辑器", False, ["def f(): pass"],
                "anchor", "正常代码不该拦"),
    PrivacyCase("p08", "Q3 复盘 - WPS", "wps.exe", False, ["营收环比增长 12%"],
                "rolling", "正常文档不该拦"),
    PrivacyCase("p09", "知乎 - Edge", "msedge.exe", False, ["如何设计 Agent 记忆系统"],
                "rolling"),
    PrivacyCase("p10", "无痕浏览 - Chrome", "chrome.exe", True, [], "challenge", "无痕窗口"),
    PrivacyCase("p11", "Bitwarden - 我的保险库", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p12", "Keepass 数据库已解锁", "KeePass.exe", True, [], "rolling"),
    PrivacyCase("p13", "LastPass Vault", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p14", "微信支付 - 转账", "WeChat.exe", True, [], "rolling"),
    PrivacyCase("p15", "PayPal - Send Money", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p16", "信用卡账单 本期应还", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p17", "私钥备份", "notepad.exe", True, ["-----BEGIN RSA PRIVATE KEY-----"],
                "challenge", "私钥"),
    PrivacyCase("p18", "接口文档 - Chrome", "chrome.exe", False, ["Authorization: Bearer <token>"],
                "challenge", "占位符不是真 token，不该误拦"),
    PrivacyCase("p19", "会议纪要.docx - WPS", "wps.exe", False, ["下周交付里程碑"],
                "rolling"),
    PrivacyCase("p20", "终端", "WindowsTerminal.exe", False, ["git status On branch main"],
                "rolling", "终端命令不该拦"),
    # ↓ 通道级期望：这几条的重点不是"整条拦不拦"，而是"**哪一部分**该拦"。
    #   旧版只能给二值结论，这些用例根本没法表达。
    PrivacyCase("p21", "main.py - Visual Studio Code", "编辑器", True,
                ["sk-abcdef1234567890abcdef1234567890"], "challenge",
                "只该拦正文；标题和骨架可以留",
                channel_expectations={"a11y_text": False, "window_meta": True, "event": True}),
    PrivacyCase("p22", "1Password - 保险库", "1password.exe", True, [], "challenge",
                "私密窗口：连骨架和标题都不留",
                channel_expectations={"event": False, "window_meta": False}),
    PrivacyCase("p23", "记事本", "notepad.exe", True, ["普通笔记内容"], "challenge",
                "卡号出现在 OCR 文本里，该拦的是 OCR 通道",
                ocr_text="6222 0212 3456 7890",
                channel_expectations={"ocr_text": False, "a11y_text": True}),
    # ---------- 第二批：覆盖面补齐 ----------
    PrivacyCase("p24", "KeePassXC - 数据库", "KeePassXC.exe", True, [], "rolling"),
    PrivacyCase("p25", "NordPass Vault", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p26", "工商银行 - 个人网上银行", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p27", "招商银行 - 一卡通", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p28", "微信 - 收付款", "WeChat.exe", True, [], "rolling"),
    PrivacyCase("p29", "云闪付 - 我的卡包", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p30", "Stripe Dashboard - Payments", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p31", "AWS 控制台 - IAM 访问密钥", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p32", "阿里云 - AccessKey 管理", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p33", "环境变量配置", "notepad.exe", True,
                ["OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345"],
                "rolling", "环境变量里的 key"),
    PrivacyCase("p34", "登录凭据", "notepad.exe", True,
                ["password: hunter2SuperSecret"], "challenge"),
    PrivacyCase("p35", "SSH 配置", "notepad.exe", True,
                ["-----BEGIN OPENSSH PRIVATE KEY-----"], "rolling"),
    PrivacyCase("p36", "护照扫描件", "wps.exe", True, ["E12345678"], "challenge"),
    PrivacyCase("p37", "病历 - 门诊记录", "chrome.exe", True, [], "challenge", "医疗信息"),
    PrivacyCase("p38", "银行流水 - 明细", "chrome.exe", True, [], "rolling"),
    PrivacyCase("p39", "微信 - 私密聊天", "WeChat.exe", True, [], "challenge"),
    # 反向：这些不该拦（误拦会让人以为工具坏了）
    PrivacyCase("p40", "index.ts - Visual Studio Code", "编辑器", False,
                ["export const API_BASE = '/v1'"], "rolling"),
    PrivacyCase("p41", "技术方案.md - Typora", "Typora.exe", False,
                ["第三章 接口设计"], "rolling"),
    PrivacyCase("p42", "GitHub - Pull requests", "chrome.exe", False,
                ["Add retry logic to client"], "rolling"),
    PrivacyCase("p43", "终端", "WindowsTerminal.exe", False,
                ["docker ps"], "rolling"),
)


class GoldenSet:
    """一份可存取的黄金集。"""

    VERSION = "1.0"

    def __init__(
        self,
        activity: list[ActivityCase] | None = None,
        privacy: list[PrivacyCase] | None = None,
        version: str = VERSION,
        created: str = "",
    ) -> None:
        self.activity = activity if activity is not None else []
        self.privacy = privacy if privacy is not None else []
        self.version = version
        self.created = created or date.today().isoformat()

    @classmethod
    def seed(cls) -> GoldenSet:
        """内置种子集：够跑起来，但真正的价值在往里加真实失败样本。"""
        return cls(activity=list(SEED_ACTIVITY), privacy=list(SEED_PRIVACY))

    # ---- 分池 ----

    def pool(self, name: str) -> list:
        if name not in POOLS:
            raise ValueError(f"未知的池：{name}，可选 {POOLS}")
        return [c for c in (*self.activity, *self.privacy) if c.pool == name]

    def holdout(self) -> list:
        """留出集：自进化的候选改进**不许看**这一部分，否则就是自己给自己出题。"""
        return self.pool("challenge")

    def dev(self) -> list:
        """开发集：允许用来调规则、找启发式。"""
        return [*self.pool("anchor"), *self.pool("rolling")]

    def add(self, case) -> None:  # noqa: ANN001
        (self.activity if isinstance(case, ActivityCase) else self.privacy).append(case)

    def by_id(self, case_id: str):
        for case in (*self.activity, *self.privacy):
            if case.case_id == case_id:
                return case
        return None

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for case in (*self.activity, *self.privacy):
            counts[case.pool] = counts.get(case.pool, 0) + 1
        return {
            "version": self.version,
            "created": self.created,
            "activity": len(self.activity),
            "privacy": len(self.privacy),
            "total": len(self.activity) + len(self.privacy),
            "pools": counts,
        }

    def coverage(self) -> dict:
        """活动类别的覆盖度——防止某类样本被淹没。"""
        seen: dict[str, int] = {a: 0 for a in ACTIVITIES}
        for case in self.activity:
            seen[case.expect] = seen.get(case.expect, 0) + 1
        return seen

    # ---- 存取 ----

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "created": self.created,
            "activity": [c.to_row() for c in self.activity],
            "privacy": [c.to_row() for c in self.privacy],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> GoldenSet:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            activity=[ActivityCase(**row) for row in data.get("activity", [])],
            privacy=[PrivacyCase(**row) for row in data.get("privacy", [])],
            version=data.get("version", cls.VERSION),
            created=data.get("created", ""),
        )

    @classmethod
    def load_or_seed(cls, path: Path) -> GoldenSet:
        path = Path(path)
        if not path.exists():
            return cls.seed()
        # 落盘版本存在时，**必须把种子里新增的样本并进来**。
        # 否则往 SEED_* 里补的样本永远进不来，数据集会悄悄停在旧版本上——
        # 这个坑踩过：加完 59 条样本跑评测，数字一个没变，因为读的还是老 json
        golden = cls.load(path)
        golden.merge_seed()
        return golden

    def merge_seed(self) -> int:
        """把种子里的新样本并进来（按 case_id 去重），返回新增条数。

        只增不删：落盘版本里可能有从飞轮晋升进来的真实样本，
        那些不该被种子覆盖掉。
        """
        existing = {case.case_id for case in (*self.activity, *self.privacy)}
        added = 0
        for case in (*SEED_ACTIVITY, *SEED_PRIVACY):
            if case.case_id in existing:
                continue
            self.add(case)
            existing.add(case.case_id)
            added += 1
        return added

    # ---- 维护（对标「黄金集会腐烂」那套）----

    def next_id(self, prefix: str) -> str:
        existing = {c.case_id for c in (*self.activity, *self.privacy)}
        index = 1
        while f"{prefix}{index:02d}" in existing:
            index += 1
        return f"{prefix}{index:02d}"
