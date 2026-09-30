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
import re
import threading
import time
from collections import deque
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


_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s\"'<>)）】、,，]+"       # 带协议或 www 的
    r"|localhost:\d+[^\s\"'<>)）】、,，]*",            # 本地开发地址（不带协议但信息量大）
    re.I,
)


def extract_url(texts: list[str], ocr_text: str = "") -> str:
    """从无障碍文本或认图结果里揪出当前网址。

    浏览器场景下「他在看什么」最直接的证据就是 URL：实测 Edge / Chrome 的地址栏会
    出现在无障碍树里（能拿到 https://www.zhihu.com），Electron 版浏览器就得靠 OCR。
    """
    blob = " ".join(texts[:60]) + " " + ocr_text[:800]
    match = _URL_RE.search(blob)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:").rstrip("/")[:200]


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
    ocr_text: str = ""          # UIA 只拿到空壳时的认图结果
    url: str = ""               # 当前网址（浏览器场景最直接的证据）
    activity: str = ""          # 活动类型（coding / meeting / ...），分类器打标
    focus: float = 0.0          # 专注度 0-1
    interruptible: bool = True  # 此刻能不能打扰
    source: str = "title"       # title / uia / ocr / uia+ocr
    skip_reason: str = ""       # 非空表示内容被隐私闸门挡下，只留骨架

    @property
    def app(self) -> str:
        return classify_app(self.process_name)

    @property
    def recorded(self) -> bool:
        return not self.skip_reason

    def digest(self, limit: int = 15) -> str:
        """压成一行，给时间线与全文检索用。

        正文取法：UIA 条数够多（原生应用、浏览器）就用它；条数太少说明只拿到空壳
        （Electron 系），这时**认图结果才是真内容**。不能因为 UIA 有「Chrome Legacy
        Window」这么一条垃圾文本就把整段 OCR 丢掉——这是实测踩出来的坑。
        """
        parts = [t for t in self.texts[:limit] if t.strip()]
        if self.ocr_text and len(parts) < 10:
            parts = [self.ocr_text]
        body = " ".join(parts)
        context = " ".join(self.other_windows())
        tail = f" {self.url}" if self.url else ""
        return f"{body[:400]} {context[:200]}{tail}".strip()

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
        use_ocr: bool = True,
        ocr_min_texts: int = 5,
        classifier=None,  # noqa: ANN001 - ActivityClassifier，注入式；不传就只做采集
        flywheel=None,    # noqa: ANN001 - Flywheel，不传就不攒难例
        flywheel_interval: float = 30.0,
        flywheel_queue_max: int = 100,
    ) -> None:
        self.privacy = privacy or PrivacyGate()
        self.use_uia = use_uia
        self.uia_timeout = uia_timeout
        self.uia_max_texts = uia_max_texts
        self.min_title_length = min_title_length
        self.snapshot_windows = snapshot_windows
        self.use_ocr = use_ocr
        self.ocr_min_texts = ocr_min_texts
        self.classifier = classifier
        self.flywheel = flywheel
        self.flywheel_interval = flywheel_interval
        self._last_flywheel_at = 0.0
        # 难例队列：满则丢最旧并计数。原先直接按节流丢弃，漏采完全不可见
        self._flywheel_queue: deque = deque(maxlen=max(1, int(flywheel_queue_max)))
        self.flywheel_dropped = 0
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

        # UIA 只拿到一个空壳（Electron / Chromium 系）→ 认图兜底。
        # 这条路的代价是截一张全屏图（实测约 370 ms），所以只在真正需要时才走。
        if self.use_ocr and len(event.texts) < self.ocr_min_texts:
            self._fill_ocr(event)

        event.url = extract_url(event.texts, event.ocr_text)

        # 内容敏感：**任一路命中就两路都清**。
        # UIA 文本和 OCR 文本读的是同一块屏幕，只清其中一路等于没清——
        # 这里是安全性优先的地方，细粒度没有意义。
        # 但走 evaluate 能拿到"是哪一路触发的"，排查时比一句笼统的"内容疑似敏感"有用得多。
        decision = self.privacy.evaluate(
            event.window_title, event.process_name, event.texts,
            ocr_text=event.ocr_text, moment=event.ts,
        )

        def _blocked(name: str) -> bool:
            verdict = decision.channel(name)
            return verdict is not None and not verdict.allowed

        a11y_blocked, ocr_blocked = _blocked("a11y_text"), _blocked("ocr_text")
        if a11y_blocked or ocr_blocked:
            event.texts = []
            event.ocr_text = ""
            trigger = "、".join(
                label for label, hit in (("无障碍文本", a11y_blocked), ("OCR 文本", ocr_blocked))
                if hit
            )
            event.skip_reason = f"内容疑似敏感（{trigger}）"

        # 类型化理解：判断「他在干什么」。用小分类器而不是大模型——
        # 实测 Jev/Laya 常驻内存后单次判断约 0.05 秒，比采集本身还便宜
        if self.classifier is not None:
            try:
                label, rule_activity = self.classifier.classify_with_rule(
                    event.digest(), event.window_title, event.app
                )
                event.activity = label.activity
                event.focus = label.focus
                event.interruptible = label.interruptible
                self._feed_flywheel(event, label, rule_activity)
            except Exception:  # noqa: BLE001 - 打标失败不影响采集
                logger.debug("活动打标失败", exc_info=True)

        return event

    def _feed_flywheel(self, event: SightEvent, label, rule_activity: str) -> None:  # noqa: ANN001
        """把可疑样本**入队**，到点再统一排空写盘。

        只喂两类，不是全量：**规则与模型打架**的、以及**系统自己也没底**的。
        全量灌进去只会淹没真正该看的样本。

        **为什么改成队列**：原先的 30 秒节流是丢弃式的——喂进一条之后，30 秒内
        出现的其他难例**直接丢掉**，漏采还完全不可见。改成有界队列后：
        样本先入队（满则丢最旧并计数），到点一次性 drain —— 既不丢样本，
        也不让写盘拖慢观察循环。
        """
        if self.flywheel is None:
            return
        item = self._build_candidate(event, label, rule_activity)
        if item is not None:
            if len(self._flywheel_queue) >= (self._flywheel_queue.maxlen or 0):
                self.flywheel_dropped += 1     # 丢弃可观测，不再无声无息
            self._flywheel_queue.append(item)
        if time.monotonic() - self._last_flywheel_at >= self.flywheel_interval:
            self.drain_flywheel()

    def _build_candidate(self, event: SightEvent, label, rule_activity: str):  # noqa: ANN001, ANN202
        """判断这条观察到不到进飞轮的标准。返回 (kind, kwargs) 或 None。

        这是纯判断，不碰 IO——真正写盘留给 `drain_flywheel`。
        """
        # 两路打架比低置信度更有价值：说明系统的两套标准本身就不一致
        if label.source == "http" and rule_activity != label.activity:
            return ("dis", {
                "text": event.digest(), "rule_guess": rule_activity,
                "model_guess": label.activity,
                "window_title": event.window_title, "app": event.app,
            })
        threshold = getattr(self.flywheel, "low_confidence", 0.55)
        if label.confidence and label.confidence < threshold:
            return ("obs", {
                "text": event.digest(), "guess": label.activity,
                "window_title": event.window_title, "app": event.app,
                "confidence": label.confidence,
            })
        return None

    def drain_flywheel(self) -> int:
        """把队列里的候选一次性写盘，返回写入条数。

        单条失败不影响后续——攒难例是副产品，不能因为一条脏数据把整批丢掉。
        """
        if self.flywheel is None:
            return 0
        written = 0
        while self._flywheel_queue:
            kind, args = self._flywheel_queue.popleft()
            try:
                if kind == "dis":
                    self.flywheel.mark_disagreement(**args)
                else:
                    self.flywheel.observe(**args)
                written += 1
            except Exception:  # noqa: BLE001
                logger.debug("写飞轮候选失败", exc_info=True)
        self._last_flywheel_at = time.monotonic()
        return written

    def _fill_ocr(self, event: SightEvent) -> None:
        from screen_agent.capture import ocr

        try:
            text = ocr.capture_and_recognize()
        except Exception:  # noqa: BLE001 - 认图失败退回只有标题
            logger.debug("OCR 兜底失败", exc_info=True)
            return
        if text:
            event.ocr_text = text[:3000]
            event.source = "uia+ocr" if event.texts else "ocr"

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
