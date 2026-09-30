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

# 默认拦掉：密码管理器、银行支付、私密通讯
DEFAULT_DENY_TITLES = (
    "1password", "bitwarden", "keepass", "lastpass", "dashlane", "keeper",
    "password", "密码", "凭据", "credential",
    "bank", "银行", "支付宝", "alipay", "微信支付", "paypal", "stripe",
    "网银", "信用卡", "转账",
    "私密", "incognito", "无痕",
)

# 疑似敏感内容：命中就整条丢弃，不做局部打码——打码后的残句照样泄露上下文
_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|ghp|gho|glpat|xox[baprs])-[A-Za-z0-9_\-]{16,}"),   # 各类 API key
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),                            # 长 base64
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),                 # 卡号
    re.compile(r"\b\d{17}[\dXx]\b"),                                        # 身份证
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),                      # 私钥
]


@dataclass
class PrivacyGate:
    """判断一条采集该不该落库。"""

    deny_titles: tuple[str, ...] = DEFAULT_DENY_TITLES
    deny_apps: tuple[str, ...] = ()
    active_hours: tuple[int, int] | None = None   # 例如 (9, 19)
    extra_deny: tuple[str, ...] = field(default_factory=tuple)

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

    def verdict(self, title: str, process_name: str, texts: list[str]) -> tuple[bool, str]:
        """一眼定生死：返回 (能不能记, 原因)。"""
        if not self.in_active_hours():
            return False, "不在记录时段"
        if self.is_private_window(title, process_name):
            return False, "私密窗口"
        joined = " ".join(texts[:50])
        if self.looks_sensitive(joined):
            return False, "内容疑似敏感"
        return True, ""
