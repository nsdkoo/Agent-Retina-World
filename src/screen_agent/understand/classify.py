"""屏幕内容的类型化理解：给每次观察打结构化标签。

思路来自 **typesafe-computer-use**（853 stars，MIT）：用 OCR + 无障碍树喂给**小分类器**
判断下一步，只在真需要自由文本时才调前沿大模型。屏幕感知同理——判断「他在干什么」
根本不需要大模型，需要的是**又快又便宜的分类器**。

用的就是手边这套类 Jev 决策模型（`jev-playground` 的 Laya 多语言权重，643 MB，CPU 可跑）。
它的抽象很干净：

    state（看到的内容） + questions（choice 多选 / score 打分 / noul 是非） → 结构化答案

实测（CPU）：首次加载 55 s（一次性），之后**每次推理 0.1 秒**——比 UIA 的 1.1 s、
OCR 的 0.37 s 都快，所以完全能跑在线。

三级后端按可用性自动降级，保证这个功能**永远有输出**：

1. `http`  本地 Jev 服务（需 jev-playground 的 server.py 在跑）—— 最快、最准
2. `rules` 纯规则兜底（零依赖、零延迟）—— 没起服务也能用
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# 问模型的问题集。三种题型各司其职：
# choice 定活动类型、score 打专注度、noul 判断能不能打扰
_QUESTIONS = {
    "activity": {
        "type": "choice",
        "instructions": "这个人此刻在做什么类型的事？",
        "criteria": {
            "coding": "写代码、调试、读代码、跑命令、看日志",
            "writing": "写文档、写方案、做表格或演示",
            "meeting": "开会、语音或视频通话",
            "reading": "读文章、查资料、看在线文档",
            "chatting": "即时通讯聊天、回消息",
            "browsing": "浏览网页、刷资讯、看视频",
            "idle": "空闲或离开",
            "other": "其他或无法判断",
        },
    },
    "focus": {
        "type": "score",
        "instructions": "专注程度",
        "criteria": [
            "频繁切换，很碎片",
            "在推进，偶有打断",
            "深度专注在一件事上",
        ],
    },
    "interruptible": {
        "type": "noul",
        "instructions": "此刻打断他是否合适",
    },
}

ACTIVITY_LABELS = {
    "coding": "写代码",
    "writing": "写文档",
    "meeting": "开会",
    "reading": "看资料",
    "chatting": "聊天",
    "browsing": "浏览网页",
    "idle": "空闲",
    "other": "其他",
}

# 关键词兜底规则：按「越具体越靠前」排，命中即停
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("meeting", ("zoom", "腾讯会议", "teams", "飞书会议", "钉钉会议", "会议", "通话中", "共享屏幕")),
    ("chatting", ("微信", "wechat", "qq", "钉钉", "飞书", "telegram", "slack", "消息", "聊天")),
    ("coding", ("visual studio", "vscode", "cursor", "pycharm", "idea", "终端", "terminal",
                "powershell", "bash", "github", "gitlab", "traceback", "def ", "import ",
                ".py", ".ts", ".vue", ".java", "编译", "报错", "调试")),
    ("writing", ("wps", "word", "excel", "powerpoint", "ppt", "文档", "方案", "表格",
                 "演示", "报表", "合同")),
    ("reading", ("pdf", "阅读", "资料", "文章", "文档中心", "知识库")),
    ("browsing", ("chrome", "edge", "firefox", "浏览器", "知乎", "微博", "bilibili",
                  "youtube", "资讯", "新闻")),
)

# 这些活动开着的时候，别去打扰
_DO_NOT_DISTURB = {"meeting"}


@dataclass
class ActivityLabel:
    activity: str = "other"
    focus: float = 0.5
    interruptible: bool = True
    confidence: float = 0.0
    source: str = "rules"

    @property
    def activity_cn(self) -> str:
        return ACTIVITY_LABELS.get(self.activity, self.activity)

    def summary(self) -> str:
        mark = "可打扰" if self.interruptible else "别打扰"
        return f"{self.activity_cn}·专注{self.focus:.0%}·{mark}（{self.source}）"


def _normalize_score(raw) -> float:  # noqa: ANN001
    """score 题的返回归一化到 0-1。模型给的是 criteria 下标（如 0.92 → 第 1 档）。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5
    if 0.0 <= value <= 1.0:
        return value
    return min(1.0, value / 3.0)


class ActivityClassifier:
    """给一段屏幕文本判活动类型。后端自动降级，永远给得出结果。"""

    def __init__(
        self,
        service_url: str = "",
        enabled: bool = True,
        timeout: float = 3.0,
        autostart: bool = False,
        project_dir: str = "",
        python_exe: str = "",
        service_port: int = 8790,
    ) -> None:
        self.service_url = (service_url or "").rstrip("/")
        self.enabled = enabled
        self.timeout = timeout
        self.autostart = autostart
        self.project_dir = project_dir
        self.python_exe = python_exe
        self.service_port = service_port
        self._http_ok: bool | None = None   # 探到一次失败就不再反复试
        self._spawned = False

    @property
    def mode(self) -> str:
        if not self.enabled:
            return "off"
        if self.service_url and self._http_ok is not False:
            return "http"
        return "rules"

    # ---- 对外 ----

    def classify(self, text: str, window_title: str = "", app: str = "") -> ActivityLabel:
        if not self.enabled:
            return ActivityLabel(source="off")
        blob = f"{app} {window_title} {text}".strip()
        if not blob:
            return ActivityLabel(activity="idle", focus=0.0, interruptible=True, source="rules")
        if self.mode == "http":
            label = self._classify_http(blob)
            if label is not None:
                return label
        return self._classify_rules(blob)

    def classify_with_rule(
        self, text: str, window_title: str = "", app: str = ""
    ) -> tuple[ActivityLabel, str]:
        """同时给出「最终判定」和「纯规则判定」。

        规则跑一次是纯字符串匹配，几乎不要钱，所以每条都跑得起。
        两路打架的样本比低置信度更值得进飞轮——它说明系统的两套标准本身就不一致。
        """
        blob = f"{app} {window_title} {text}".strip()
        rule_activity = self._classify_rules(blob).activity if blob else "idle"
        return self.classify(text, window_title, app), rule_activity

    # ---- 规则兜底 ----

    @staticmethod
    def _classify_rules(blob: str) -> ActivityLabel:
        lowered = blob.lower()
        for activity, hints in _RULES:
            if any(hint in lowered for hint in hints):
                return ActivityLabel(
                    activity=activity,
                    focus=0.6,
                    interruptible=activity not in _DO_NOT_DISTURB,
                    confidence=0.4,
                    source="rules",
                )
        return ActivityLabel(source="rules")

    # ---- 服务生命周期：常驻是关键 ----

    def probe(self) -> tuple[bool, str]:
        """探一次服务状态，返回 (是否就绪, 说明)。

        为什么要单独探就绪：模型加载是**进程级**的一次性开销（冷 55 秒 / 热 7 秒），
        加载完成前调用只会超时。调用方据此决定「先用规则顶着」还是「切到模型」。
        """
        if not self.service_url:
            return False, "没有配置分类服务"
        try:
            with urlopen(f"{self.service_url}/api/status", timeout=self.timeout) as response:
                data = json.load(response)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            self._http_ok = False
            return False, "服务没在跑"
        if not data.get("provider"):
            return False, "服务在跑，但没有可用模型"
        if not data.get("ready"):
            return False, "模型还在加载"
        self._http_ok = True
        return True, f"{data['provider']} 就绪"

    def ensure_service(self) -> tuple[bool, str]:
        """服务没起就后台拉一个——**启动时加载一次，之后常驻**。

        这是这套方案唯一正确的打开方式：模型常驻内存后每次推理只要 0.05 秒；
        而每起一次新进程都要重付加载成本（冷 55 秒 / 热 7 秒），根本没法用。
        """
        ready, note = self.probe()
        if ready:
            return True, note
        if not (self.autostart and self.project_dir and not self._spawned):
            return False, note

        script = os.path.join(self.project_dir, "server.py")
        if not os.path.isfile(script):
            return False, f"没找到分类服务脚本：{script}"
        exe = self.python_exe or os.path.join(
            self.project_dir, ".venv", "Scripts", "python.exe"
        )
        if not os.path.isfile(exe):
            exe = "python"
        env = dict(os.environ, JEV_PORT=str(self.service_port))
        try:
            subprocess.Popen(
                [exe, script],
                cwd=self.project_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return False, f"拉起服务失败：{exc}"
        self._spawned = True
        if not self.service_url:
            self.service_url = f"http://127.0.0.1:{self.service_port}"
        return False, "已拉起服务，模型正在加载（这段时间先用规则顶着）"

    # ---- 本地 Jev 服务 ----

    def _classify_http(self, blob: str) -> ActivityLabel | None:
        payload = json.dumps({"state": blob[:2000], "questions": _QUESTIONS}).encode("utf-8")
        request = Request(
            f"{self.service_url}/api/ask",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            if self._http_ok is not False:
                logger.debug("Jev 分类服务不可用，退回规则", exc_info=True)
            self._http_ok = False
            return None
        self._http_ok = True
        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> ActivityLabel | None:
        answers = (data or {}).get("answers")
        if not isinstance(answers, dict):
            return None
        label = ActivityLabel(source="http")

        activity = answers.get("activity")
        if isinstance(activity, dict):
            picked = str(activity.get("choice") or "").strip().lower()
            if picked in ACTIVITY_LABELS:
                label.activity = picked
            try:
                label.confidence = float(activity.get("answer_confidence")
                                         or activity.get("confidence") or 0.0)
            except (TypeError, ValueError):
                label.confidence = 0.0

        focus = answers.get("focus")
        if isinstance(focus, dict):
            label.focus = _normalize_score(focus.get("score"))

        noul = answers.get("interruptible")
        if isinstance(noul, dict):
            try:
                label.interruptible = float(noul.get("noul") or 0) >= 0.5
            except (TypeError, ValueError):
                label.interruptible = True

        # 开会时不管模型怎么说，一律标记为别打扰
        if label.activity in _DO_NOT_DISTURB:
            label.interruptible = False
        return label
