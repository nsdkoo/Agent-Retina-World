"""桌面观察者：前台一变就记一笔。

对标 Screenpipe 的 event-driven capture——**不空转、不定时截图**。

为什么是「窗口变了才记」而不是定时拍：

- 定时截图（比如每 30 秒）在你看同一个页面五分钟时白拍十张，还全是重复的
- 真正有价值的信息是「你从什么切到了什么」——那是事件，不是帧
- 事件驱动下 CPU 只在切换那一瞬间动一下，其余时间完全安静

采集分三层，便宜的在前面：

1. **窗口标题 + 进程名**——几乎零成本，每次都给
2. **UIA 结构化文本**——只在窗口切换时调一次（约 1 秒），拿到屏幕上真实的字
3. **截图**——默认不拍。UIA 拿不到内容的应用（游戏、远程桌面）才考虑兜底

隐私闸门在采集之前：私密窗口连标题都不留。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from screen_agent.capture.context import get_foreground_context
from screen_agent.capture.privacy import PrivacyGate

logger = logging.getLogger(__name__)

# 应用名归类：把 window title 归到"你在用什么"
APP_HINTS = {
    "chrome": "浏览器", "msedge": "浏览器", "firefox": "浏览器", "brave": "浏览器",
    "code": "编辑器", "cursor": "编辑器", "qoder": "编辑器", "pycharm": "编辑器",
    "idea": "编辑器", "webstorm": "编辑器", "sublime_text": "编辑器", "notepad": "编辑器",
    "windowsterminal": "终端", "powershell": "终端", "cmd": "终端", "wt": "终端",
    "wechat": "微信", "qq": "QQ", "dingtalk": "钉钉", "wxwork": "企业微信",
    "feishu": "飞书", "telegram": "Telegram",
    "excel": "表格", "wps": "办公", "winword": "文档", "powerpnt": "演示",
    "explorer": "文件管理", "code": "编辑器",
    "zoom": "会议", "teams": "会议", "tencentmeeting": "会议",
}


def classify_app(process_name: str) -> str:
    """进程名 → 人话应用名。认不出来就原样返回。"""
    stem = (process_name or "").lower().removesuffix(".exe")
    if not stem:
        return "未知"
    for key, label in APP_HINTS.items():
        if key in stem:
            return label
    return stem


# 系统窗口：记了没用，还占地方
_SYSTEM_WINDOW_HINTS = (
    "program manager", "windows 输入体验", "microsoft text input",
    "设置", "settings", "default ime", "windows shell experience",
)


def interesting_windows(titles: list[str]) -> list[str]:
    """滤掉系统窗口，去重、保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for title in titles:
        clean = (title or "").strip()
        if not clean or clean in seen:
            continue
        if any(hint in clean.lower() for hint in _SYSTEM_WINDOW_HINTS):
            continue
        seen.add(clean)
        out.append(clean)
    return out


@dataclass
class SightEvent:
    """一次「看到了什么」的快照。"""

    ts: datetime
    window_title: str
    process_name: str
    texts: list[str] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)   # 同屏还开着哪些窗口
    source: str = "title"       # title / uia / screenshot
    skip_reason: str = ""       # 非空表示内容被隐私闸门挡下，只留骨架

    @property
    def app(self) -> str:
        return classify_app(self.process_name)

    @property
    def recorded(self) -> bool:
        return not self.skip_reason

    def digest(self, limit: int = 15) -> str:
        """压成一行，给时间线与全文检索用。含同屏窗口名——
        「他同时在忙什么」往往就写在这些标题里。"""
        body = " ".join(t for t in self.texts[:limit] if t.strip())
        context = " ".join(self.other_windows())
        return f"{body[:400]} {context[:200]}".strip()

    def other_windows(self, limit: int = 8) -> list[str]:
        """除前台之外还开着的窗口。"""
        return [w for w in self.windows if w and w != self.window_title][:limit]

    def summary(self) -> str:
        head = f"{self.app}｜{self.window_title or '(无标题)'}"
        others = self.other_windows(3)
        if others:
            head += f"（同屏还有：{'、'.join(others)}）"
        if self.skip_reason:
            return f"{head} — {self.skip_reason}，内容未记录"
        return head


class DesktopWatcher:
    """轮询前台窗口，只在切换时产出一条事件。"""

    def __init__(
        self,
        privacy: PrivacyGate | None = None,
        use_uia: bool = True,
        uia_timeout: float = 8.0,
        uia_max_texts: int = 200,
        min_title_length: int = 1,
        snapshot_windows: bool = True,
    ) -> None:
        self.privacy = privacy or PrivacyGate()
        self.use_uia = use_uia
        self.uia_timeout = uia_timeout
        self.uia_max_texts = uia_max_texts
        self.min_title_length = min_title_length
        self.snapshot_windows = snapshot_windows
        self._last_key: tuple[str, str] | None = None
        self._last_seen: datetime | None = None

    # ---- 单次观察 ----

    def poll(self) -> SightEvent | None:
        """看一眼前台。没变化返回 None；变了返回一条事件（可能被隐私闸门标记）。"""
        from screen_agent.capture import uia

        hwnd = uia.foreground_hwnd()
        ctx = get_foreground_context()
        if ctx is None:
            return None
        title = (ctx.window_title or "").strip()
        process = (ctx.process_name or "").strip()
        if len(title) < self.min_title_length and not process:
            return None

        key = (title, process)
        if key == self._last_key:
            return None
        self._last_key = key
        self._last_seen = datetime.now()

        event = SightEvent(ts=self._last_seen, window_title=title, process_name=process)

        # 不在记录时段：连事件都不产
        if not self.privacy.in_active_hours(event.ts):
            event.skip_reason = "不在记录时段"
            return event

        # 私密窗口：不调 UIA、不留标题，只留「某时刻用过某应用」这个骨架
        if self.privacy.is_private_window(title, process):
            event.skip_reason = "私密窗口"
            event.window_title = ""
            return event

        # 同屏窗口快照：这一层比 UIA 稳得多——Electron / Chromium 系应用内部读不到，
        # 但窗口标题一直在，而「他同时在忙什么」往往就写在这些标题里
        if self.snapshot_windows:
            event.windows = self._snapshot_windows()

        if self.use_uia:
            self._fill_texts(event, hwnd)

        if event.texts and self.privacy.looks_sensitive(" ".join(event.texts[:50])):
            event.texts = []
            event.skip_reason = "内容疑似敏感"

        return event

    @staticmethod
    def _snapshot_windows() -> list[str]:
        """当前所有可见窗口标题。非 Windows 或枚举失败时返回空表。"""
        try:
            from screen_agent.tools import _win
        except Exception:  # noqa: BLE001 - 非 Windows 平台
            return []
        try:
            titles = [w.get("title", "") for w in _win.list_windows()]
        except Exception:  # noqa: BLE001
            logger.debug("窗口枚举失败", exc_info=True)
            return []
        return interesting_windows(titles)

    def _fill_texts(self, event: SightEvent, hwnd: int = 0) -> None:
        from screen_agent.capture import uia

        try:
            content = uia.read_foreground_text(
                timeout=self.uia_timeout, max_texts=self.uia_max_texts, hwnd=hwnd
            )
        except Exception:  # noqa: BLE001 - 读不到就只留标题，不能让观察循环挂掉
            logger.debug("UIA 读取异常", exc_info=True)
            return
        if content is None:
            return
        if content.texts:
            event.texts = content.texts
            event.source = "uia"
        if not event.window_title and content.title:
            event.window_title = content.title

    # ---- 常驻循环 ----

    def run_forever(
        self,
        on_event,  # noqa: ANN001 - Callable[[SightEvent], None]
        stop_event: threading.Event,
        interval: float = 1.0,
    ) -> None:
        """一直看着。stop_event 一置位就干净退出。"""
        while not stop_event.wait(interval):
            try:
                event = self.poll()
            except Exception:  # noqa: BLE001
                logger.debug("观察循环单次出错", exc_info=True)
                continue
            if event is None:
                continue
            try:
                on_event(event)
            except Exception:  # noqa: BLE001 - 落库失败不能拖垮观察
                logger.debug("观察事件处理失败", exc_info=True)

    def reset(self) -> None:
        """忘掉上一个窗口，下次 poll 一定产出事件（用于手动触发采集）。"""
        self._last_key = None
