"""隐私闸门：常驻记录屏幕，先把「哪些不能记」钉死。

对标 Screenpipe 的窗口过滤与 PII 处理——全天候录制的东西一旦漏了密码框，
就不是功能问题而是事故。三条防线：

1. **窗口/应用黑名单**：密码管理器、银行、支付页面直接跳过，连标题都不留
2. **敏感内容模式**：文本里出现疑似密钥、卡号、身份证号就整条丢弃
3. **时间窗口**：可以只在你上班时段记录（`active_hours`），下班自动歇着

黑名单用子串匹配而不是正则——配置是给人写的，写错一个正则就漏一整类，
写错一个子串最多多记一条。宁可多漏，不可错记。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as clock

# 默认拦掉：密码管理器、银行支付、凭据、证照、医疗
# 扩充依据来自黄金集评测：第一批样本全在旧名单里，所以召回的 100% 是假象；
# 样本一铺开就露出 8 条漏放（vault / 云闪付 / accesskey / 护照 / 病历…）
DEFAULT_DENY_TITLES = (
    "1password", "bitwarden", "keepass", "keepassxc", "lastpass", "dashlane",
    "keeper", "nordpass", "vault", "保险库",
    "password", "密码", "凭据", "credential",
    "bank", "银行", "支付宝", "alipay", "微信支付", "收付款", "paypal", "stripe",
    "网银", "信用卡", "转账", "云闪付", "银联",
    "accesskey", "访问密钥", "secret key", "私钥",
    "护照", "身份证", "驾照", "行驶证",
    "病历", "门诊", "住院", "诊断报告", "体检报告",
    "私密", "incognito", "无痕",
)

# 疑似敏感内容：命中就整条丢弃，不做局部打码——打码后的残句照样泄露上下文
_SECRET_PATTERNS = [
    # 各类 API key。分隔符是 `[-_]` 两种：OpenAI 用 `sk-`、GitHub 用 `ghp_`，
    # 只写连字符会把 GitHub 的 PAT 整类漏掉（这个 bug 是测试抓出来的）
    re.compile(r"\b(sk|pk|ghp|gho|glpat|xox[baprs])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),                            # 长 base64
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),                 # 卡号
    re.compile(r"\b\d{17}[\dXx]\b"),                                        # 身份证
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),                      # 私钥
]


# 可分别管控的通道。
# 为什么要把"一条记录"拆成通道：旧版是**一刀切**的二值结论——要么整条记、要么整条丢，
# 而实际需要常常是"标题可以留、正文不能留"或者"这条可以记、但别存截图"。
# 对标 Screenpipe 的 per-pipe 权限（allow-apps / deny-apps / time-range 三层执行）。
CHANNELS = ("event", "window_meta", "a11y_text", "ocr_text", "screenshot")


@dataclass(frozen=True)
class ChannelPolicy:
    """单个通道的策略。留空表示沿用闸门的全局设置。"""

    allow_apps: tuple[str, ...] = ()      # 白名单：非空时只放行这些
    deny_apps: tuple[str, ...] = ()
    active_hours: tuple[int, int] | None = None
    redact_secrets: bool = False          # True=打码保骨架；False=整通道丢弃


@dataclass
class ChannelVerdict:
    channel: str
    allowed: bool
    reason: str = ""
    # 不参与的通道（比如没启用的截图）不该影响整体放行判定——
    # 否则 allowed 会永远是 False，把兼容性直接搞坏
    participates: bool = True


@dataclass
class PrivacyDecision:
    """一条采集在各通道上的裁决结果。"""

    channels: dict[str, ChannelVerdict] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        """**所有参与判定的通道**都放行才算放行。"""
        return all(v.allowed for v in self.channels.values() if v.participates)

    @property
    def reason(self) -> str:
        """首个被拦原因。顺序从粗到细，保证报出来的原因最好懂。"""
        for name in CHANNELS:
            verdict = self.channels.get(name)
            if verdict is not None and verdict.participates and not verdict.allowed:
                return verdict.reason
        return ""

    def blocked_channels(self) -> list[str]:
        return [n for n, v in self.channels.items() if v.participates and not v.allowed]

    def channel(self, name: str) -> ChannelVerdict | None:
        return self.channels.get(name)


@dataclass
class PrivacyGate:
    """判断一条采集该不该落库，以及**每个通道**分别该不该留。"""

    deny_titles: tuple[str, ...] = DEFAULT_DENY_TITLES
    deny_apps: tuple[str, ...] = ()
    active_hours: tuple[int, int] | None = None   # 例如 (9, 19)
    extra_deny: tuple[str, ...] = field(default_factory=tuple)
    # 截图默认不采集：现在整条链路根本没有存图，这个通道先做成**策略钩子**——
    # 能对它给出裁决，但不建真实的截图存储。要真采得先给 SightEvent 加图像字段。
    screenshot_enabled: bool = False
    policies: dict[str, ChannelPolicy] = field(default_factory=dict)

    def is_private_window(self, title: str, process_name: str = "") -> bool:
        """窗口标题或进程名命中黑名单——这种连标题都不该留。"""
        blob = f"{title} {process_name}".lower()
        if not blob.strip():
            return False
        for needle in (*self.deny_titles, *self.extra_deny):
            if needle and needle.lower() in blob:
                return True
        for app in self.deny_apps:
            if app and app.lower() in process_name.lower():
                return True
        return False

    def looks_sensitive(self, text: str) -> bool:
        """文本里疑似有密钥/卡号/私钥。"""
        if not text:
            return False
        return any(pattern.search(text) for pattern in _SECRET_PATTERNS)

    def in_active_hours(self, moment: datetime | None = None) -> bool:
        if self.active_hours is None:
            return True
        moment = moment or datetime.now()
        start, end = self.active_hours
        now: clock = moment.time()
        if start <= end:
            return clock(start, 0) <= now < clock(end, 0)
        return now >= clock(start, 0) or now < clock(end, 0)   # 跨夜

    # ---- 通道级裁决 ----

    def _policy_for(self, channel: str) -> ChannelPolicy:
        return self.policies.get(channel) or ChannelPolicy()

    def evaluate(
        self,
        title: str,
        process_name: str,
        texts: list[str] | None = None,
        *,
        ocr_text: str = "",
        moment: datetime | None = None,
    ) -> PrivacyDecision:
        """逐通道裁决。

        默认行为与旧的 `verdict` **完全等价**：不在时段、私密窗口、内容敏感这三种情况
        整体都不放行。区别只在于现在能看出是**哪一层的哪一部分**被拦了——
        旧版只有一句"不能记"，你没法知道是标题不能留还是正文不能留。
        """
        texts = texts or []
        decision = PrivacyDecision()
        joined = " ".join(texts[:50])

        in_hours = self.in_active_hours(moment)
        private = self.is_private_window(title, process_name)
        sensitive = self.looks_sensitive(joined) if joined else False
        ocr_sensitive = self.looks_sensitive(ocr_text) if ocr_text else False

        # 骨架通道：它被拦 = 这条事件根本不该存在（连骨架都不留）
        if not in_hours:
            decision.channels["event"] = ChannelVerdict("event", False, "不在记录时段")
        elif private:
            decision.channels["event"] = ChannelVerdict("event", False, "私密窗口")
        else:
            decision.channels["event"] = ChannelVerdict("event", True, "")

        # 窗口元数据：标题与进程名
        decision.channels["window_meta"] = ChannelVerdict(
            "window_meta",
            not (private or not in_hours),
            "私密窗口" if private else ("不在记录时段" if not in_hours else ""),
        )
        # 文本通道分两条：无障碍文本和 OCR 文本来源不同，敏感判定也该分开算
        decision.channels["a11y_text"] = ChannelVerdict(
            "a11y_text", not sensitive, "内容疑似敏感" if sensitive else ""
        )
        decision.channels["ocr_text"] = ChannelVerdict(
            "ocr_text", not ocr_sensitive, "内容疑似敏感" if ocr_sensitive else ""
        )

        # 截图：策略钩子
        if self.screenshot_enabled:
            blocked = private or not in_hours or sensitive or ocr_sensitive
            decision.channels["screenshot"] = ChannelVerdict(
                "screenshot", not blocked, "含敏感内容" if blocked else ""
            )
        else:
            decision.channels["screenshot"] = ChannelVerdict(
                "screenshot", False, "未启用截图采集", participates=False
            )
        return decision

    def verdict(self, title: str, process_name: str, texts: list[str]) -> tuple[bool, str]:
        """兼容包装：一眼定生死，返回 (能不能记, 原因)。

        新代码请用 `evaluate()` 拿通道级结果。这个留着是为了不让既有调用点
        （评测运行器、进化回路、实测脚本）被迫改动。
        """
        decision = self.evaluate(title, process_name, texts)
        return decision.allowed, decision.reason
