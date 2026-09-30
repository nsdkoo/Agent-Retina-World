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
        return cls.load(path) if path.exists() else cls.seed()

    # ---- 维护（对标「黄金集会腐烂」那套）----

    def next_id(self, prefix: str) -> str:
        existing = {c.case_id for c in (*self.activity, *self.privacy)}
        index = 1
        while f"{prefix}{index:02d}" in existing:
            index += 1
        return f"{prefix}{index:02d}"
