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
    """

    case_id: str
    title: str
    process: str
    expect_blocked: bool
    texts: list[str] = field(default_factory=list)
    pool: str = "anchor"
    note: str = ""

    def to_row(self) -> dict:
        return asdict(self)


# ---- 种子样本：覆盖八类活动 + 几处易混边界 ----
SEED_ACTIVITY: tuple[ActivityCase, ...] = (
    ActivityCase("a01", "def analyze(self): raise ValueError index out of range", "coding",
                 "main.py - Visual Studio Code", "编辑器", "anchor", "典型报错现场"),
    ActivityCase("a02", "Traceback (most recent call last) File pytest fixtures", "coding",
                 "pytest - 终端", "终端", "rolling"),
    ActivityCase("a03", "git status On branch main nothing to commit", "coding",
                 "终端", "终端", "rolling", "只读命令也算在干活"),
    ActivityCase("a04", "与会人员 张三 李四 结论 排期顺延一周 行动项", "writing",
                 "会议纪要.docx - WPS", "办公", "anchor", "文档≠开会，这是易混点"),
    ActivityCase("a05", "产品需求说明书 第三章 功能清单 表格", "writing",
                 "需求.md - Typora", "编辑器", "rolling"),
    ActivityCase("a06", "腾讯会议 共享屏幕中 麦克风 摄像头 离开会议", "meeting",
                 "腾讯会议", "会议", "anchor", "真开会"),
    ActivityCase("a07", "Zoom Meeting Participants Chat Raise Hand", "meeting",
                 "Zoom Meeting", "Zoom", "rolling"),
    ActivityCase("a08", "如何设计 Agent 记忆系统 向量检索 长上下文", "reading",
                 "知乎 - Microsoft Edge", "浏览器", "rolling"),
    ActivityCase("a09", "Qwen2.5-VL 技术报告 第 3 节 实验设置", "reading",
                 "arxiv.org - Chrome", "浏览器", "anchor", "看资料≠写文档"),
    ActivityCase("a10", "张三：明天上午评审改到三点 李四：收到", "chatting",
                 "微信", "WeChat.exe", "anchor"),
    ActivityCase("a11", "群聊 项目组 未读消息 99+", "chatting",
                 "钉钉", "DingTalk.exe", "rolling"),
    ActivityCase("a12", "热搜榜 今日头条 推荐 视频 播放", "browsing",
                 "首页 - Microsoft Edge", "浏览器", "rolling"),
    ActivityCase("a13", "B站 番剧 追番 弹幕 播放列表", "browsing",
                 "哔哩哔哩", "浏览器", "rolling"),
    ActivityCase("a14", "", "idle", "", "", "challenge", "空内容应判空闲"),
    ActivityCase("a15", "设置 系统 显示 网络 蓝牙 声音", "other",
                 "设置", "SystemSettings.exe", "challenge", "系统设置不属于任何工作类型"),
    # 边界：同一个应用里的两种活动
    ActivityCase("a16", "Cursor Agent 正在生成代码 diff 应用", "coding",
                 "Cursor", "编辑器", "challenge", "编辑器里看 AI 写代码"),
    ActivityCase("a17", "微信文件传输助手 发送了一个 报告.pdf", "chatting",
                 "文件传输助手", "WeChat.exe", "challenge", "发文件仍是聊天场景"),
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
