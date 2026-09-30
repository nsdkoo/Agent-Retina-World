"""数据飞轮：把使用过程变成评测集，让系统越用越准。

对标 2026 年生产团队的实践，飞轮转起来就四步：

    采集 → 挖难例 → 人工确认 → 晋升进黄金集

三条纪律：

1. **来源是真实使用，不是编的**。真实用户会打错字、一句话问两件事、贴一坨日志；
   编出来的样本只编码了我们的想象。
2. **难例优先**。低置信度、规则与模型打架、被用户纠正过的——这些信息量最大。
   全量灌数据只会淹没真正该看的样本。
3. **候选不自动进黄金集**。先落 pending，确认后才晋升。
   一旦把错标灌进评测集，评测就永久失真了，而且很难发现。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from screen_agent.eval.dataset import ActivityCase, GoldenSet

# 低于这个置信度就当作「系统自己也没底」，值得人工看一眼
LOW_CONFIDENCE = 0.55

SOURCES = ("low_confidence", "disagreement", "correction", "uncovered")


@dataclass
class Candidate:
    """待确认的候选样本。"""

    text: str
    guess: str                     # 系统当前判定
    window_title: str = ""
    app: str = ""
    source: str = "low_confidence"
    confidence: float = 0.0
    corrected: str = ""            # 用户纠正后的答案（空表示还没人确认）
    seen_count: int = 1
    first_seen: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def key(self) -> str:
        return f"{self.window_title}␟{self.text[:120]}"

    @property
    def truth(self) -> str:
        """确认过的答案优先；没有就用系统判定（但这不算 ground truth）。"""
        return self.corrected or self.guess


class Flywheel:
    """候选池 + 晋升通道。落盘成 JSON，进程重启不丢。"""

    def __init__(self, path: Path, low_confidence: float = LOW_CONFIDENCE) -> None:
        self.path = Path(path)
        self.low_confidence = low_confidence
        self._candidates: dict[str, Candidate] = {}
        self._load()

    # ---- 采集 ----

    def observe(self, text: str, guess: str, window_title: str = "", app: str = "",
                confidence: float = 0.0, source: str = "low_confidence") -> Candidate | None:
        """从一次真实观察里挑候选。**只有值得看的才留**，不是全量入库。"""
        text = (text or "").strip()
        if len(text) < 8:
            return None
        interesting = confidence < self.low_confidence or source in ("disagreement", "correction")
        if not interesting:
            return None
        candidate = Candidate(
            text=text[:400], guess=guess, window_title=window_title, app=app,
            source=source, confidence=confidence,
        )
        existing = self._candidates.get(candidate.key)
        if existing is not None:
            existing.seen_count += 1          # 反复出现说明是高频场景，更该看
            return existing
        self._candidates[candidate.key] = candidate
        self._save()
        return candidate

    def mark_disagreement(self, text: str, rule_guess: str, model_guess: str,
                          window_title: str = "", app: str = "") -> Candidate | None:
        """规则和模型给出了不同答案——这类样本比低置信度更值得看。"""
        if rule_guess == model_guess:
            return None
        return self.observe(
            text, model_guess, window_title, app,
            confidence=0.0, source="disagreement",
        )

    def correct(self, text: str, corrected: str, window_title: str = "") -> Candidate | None:
        """记录用户的纠正。这是飞轮里**价值最高**的信号——直接就是标注。"""
        text = (text or "").strip()
        if not text or not corrected:
            return None
        key = f"{window_title}␟{text[:120]}"
        candidate = self._candidates.get(key)
        if candidate is None:
            candidate = Candidate(text=text[:400], guess="", window_title=window_title,
                                  source="correction")
            self._candidates[key] = candidate
        candidate.corrected = corrected
        candidate.source = "correction"
        self._save()
        return candidate

    # ---- 查看与晋升 ----

    def pending(self, only_confirmed: bool = False, limit: int = 50) -> list[Candidate]:
        items = list(self._candidates.values())
        if only_confirmed:
            items = [c for c in items if c.corrected]
        # 高频 + 已确认的排前面：它们对评测最有用
        items.sort(key=lambda c: (not c.corrected, -c.seen_count))
        return items[:limit]

    def promote(self, golden: GoldenSet, candidate: Candidate, pool: str = "rolling") -> ActivityCase:
        """把确认过的候选晋升进黄金集。

        只有带 `corrected` 的才该走这一步——**系统自己的判定不能当 ground truth**，
        否则错标会自我循环，越跑越偏。
        """
        case = ActivityCase(
            case_id=golden.next_id("fw"),
            text=candidate.text,
            expect=candidate.truth,
            window_title=candidate.window_title,
            app=candidate.app,
            pool=pool,
            note=f"来自飞轮（{candidate.source}，见到 {candidate.seen_count} 次）",
        )
        golden.add(case)
        self._candidates.pop(candidate.key, None)
        self._save()
        return case

    def promote_confirmed(self, golden: GoldenSet, pool: str = "rolling") -> list[ActivityCase]:
        """批量晋升所有已确认的候选。"""
        return [self.promote(golden, c, pool) for c in self.pending(only_confirmed=True)]

    # ---- 统计 ----

    def stats(self) -> dict:
        items = list(self._candidates.values())
        return {
            "pending": len(items),
            "confirmed": sum(1 for c in items if c.corrected),
            "by_source": dict(Counter(c.source for c in items)),
            "top_repeated": [
                {"window": c.window_title, "guess": c.guess, "seen": c.seen_count}
                for c in sorted(items, key=lambda c: -c.seen_count)[:5]
            ],
        }

    # ---- 存取 ----

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for row in rows:
            try:
                candidate = Candidate(**row)
            except TypeError:
                continue
            self._candidates[candidate.key] = candidate

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rows = [asdict(c) for c in self._candidates.values()]
            self.path.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
