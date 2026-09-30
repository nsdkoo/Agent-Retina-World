"""主动准备：在合适的时候，提出**具体**能帮上的事。

## 主动 ≠ 打扰

多数「主动式助手」的做法是定时跳出来说点什么——那是打扰，不是主动。

真正的主动要同时满足三条，**少任何一条都会变成噪声**：

1. **知道有什么事值得做** —— 你交代过但没做的 / 上次没做完的 / 你的习惯
2. **知道现在是不是时候** —— 用户正忙着就别插嘴（`interruptible` 就是干这个的）
3. **说得出具体做什么** —— 不是「要不要我帮你整理」，而是「上次那个归档没做完，接着做吗」

第 3 条最容易被忽略。含糊的建议等于把决策成本又推回给用户，
那还不如不说。

## 三路来源

| 来源 | 说什么 | 数据在哪 |
| --- | --- | --- |
| 未完成的意图 | 「你交代过 X」 | `memory.intentions` |
| 中断的任务 | 「上次那个没做完」 | `trajectory` 里 PAUSED / FAILED 的任务 |
| 习惯 | 「你平时这时候会做 X」 | episodes 的 `evidence_paths`（动过的目录/文件） |

## 为什么不用「定时」触发

定时是最省事的实现，也是最容易让人关掉通知的实现。
时机应该由**用户当时的状态**决定，不是由钟表决定。
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# 习惯要出现多少次才算「习惯」。设低了会把偶发动作当规律推荐，
# 反而显得很蠢；设高了又永远学不到东西。3 次是个务实的起点
_HABIT_MIN_OCCURRENCES = 3


def should_speak_now(
    *,
    interruptible: bool,
    in_session: bool,
    seconds_since_last: float,
    min_interval: float,
) -> bool:
    """该不该开口。

    **抽成纯函数是为了能被直接测** —— 时机判断是这套机制里最该守住的地方：
    它做坏了不会报错，只会让用户默默把通知关掉，而你还以为一切正常。

    四条都过才开口：
    1. 用户不忙 —— 最重要，忙时说啥都是打扰
    2. 不在会话中 —— 正在对话时插嘴会打断思路
    3. 距上次够久 —— 提得太勤等于骚扰
    4. （这一条由调用方保证）真有可说的事
    """
    if not interruptible:
        return False
    if in_session:
        return False
    return seconds_since_last >= min_interval


@dataclass
class Suggestion:
    """一条「可以帮你做的事」。

    `what` 必须**具体到能直接下判断**，否则就是在把决策成本推回给用户。
    """

    kind: str                       # intention / resume / habit
    what: str                       # 具体建议做什么
    why: str                        # 为什么现在提这个
    confidence: float = 0.5
    steps: list[str] = field(default_factory=list)
    # 点击后要执行的**动作**。None = 只能看，不能一键执行。
    #
    # 用结构化 dict 而不是拼一句自然语言：动作最终要按下标参数调用真实方法
    # （`resume_from(task_id)` 这类），拼字符串再解析回来是绕远路，还容易解析错。
    # 结构：{"kind": "resume"|"intention"|"run", ...}
    action: dict | None = None

    def render(self) -> str:
        return f"{self.what}（{self.why}）"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "what": self.what, "why": self.why,
            "confidence": self.confidence, "steps": list(self.steps),
            "action": dict(self.action) if self.action else None,
        }


class PreparationService:
    """算出「现在能帮上什么忙」。

    刻意只**提议**、不**执行**——主动不等于自作主张。
    真动手仍然要过 agent 的规划与审批门。
    """

    def __init__(self, memory=None, trajectory=None, max_suggestions: int = 3) -> None:  # noqa: ANN001
        self.memory = memory
        self.trajectory = trajectory
        self.max_suggestions = max_suggestions
        self.last_suggestions: list[Suggestion] = []

    def suggest(self, *, interruptible: bool = True, now: datetime | None = None) -> list[Suggestion]:
        """看现在能帮上什么。

        `interruptible=False` 时**直接返回空表**——用户正忙，说什么都是打扰。
        这个门控是整套主动机制的开关，不是可选优化。
        """
        if not interruptible:
            return []

        found: list[Suggestion] = []
        # 按「确定性」排序：用户明确交代过的 > 明确中断的 > 从习惯推断的
        found.extend(self._from_intentions(now))
        found.extend(self._from_interrupted_tasks())
        found.extend(self._from_habits(now))

        found.sort(key=lambda s: -s.confidence)
        self.last_suggestions = found[: self.max_suggestions]
        return self.last_suggestions

    # ---- 三路来源 ----

    def _from_intentions(self, now: datetime | None) -> list[Suggestion]:
        """用户明确交代过的事——确定性最高，也最该提。"""
        if self.memory is None:
            return []
        try:
            pending = self.memory.list_intentions(status="pending", limit=5)
        except Exception:  # noqa: BLE001
            logger.debug("读意图失败", exc_info=True)
            return []

        now = now or datetime.now()
        out: list[Suggestion] = []
        for item in pending:
            content = (item.get("content") or "").strip()
            if not content:
                continue
            due = (item.get("due_at") or "")[:16]
            overdue = bool(due and due < now.strftime("%Y-%m-%dT%H:%M"))
            why = f"你之前交代过，已经过了 {due}" if overdue else "你之前交代过"
            out.append(Suggestion(
                kind="intention", what=content, why=why,
                confidence=0.9 if overdue else 0.8,
                # 用户交代过的事，点一下就当指令交出去——规划器会照常拆解
                action={"kind": "intention", "content": content},
            ))
        return out

    def _from_interrupted_tasks(self) -> list[Suggestion]:
        """上次没做完的任务——「接着做」比「从零开始」省事得多。"""
        if self.trajectory is None:
            return []
        try:
            recent = self.trajectory.recent(limit=10)
        except Exception:  # noqa: BLE001
            logger.debug("读轨迹失败", exc_info=True)
            return []

        out: list[Suggestion] = []
        for row in recent:
            state = str(row.get("state") or "")
            if state not in ("paused", "failed", "cancelled"):
                continue
            goal = (row.get("goal") or "").strip()
            if not goal:
                continue
            task_id = row.get("task_id") or ""
            label = {"paused": "停在等你确认那里", "failed": "上次没跑完", "cancelled": "上次被叫停了"}
            out.append(Suggestion(
                kind="resume",
                what=f"接着做「{goal}」",
                why=f"{label.get(state, '上次没做完')}，可以直接从中断的地方续上",
                confidence=0.75,
                steps=[f"resume_from:{task_id}"] if task_id else [],
                # 续跑要调 `resume_from(task_id)` 这个真实方法，不是发一句指令
                action={"kind": "resume", "task_id": task_id} if task_id else None,
            ))
        return out

    def _from_habits(self, now: datetime | None) -> list[Suggestion]:
        """从历史 episode 里提炼「你常在哪干活」。

        **原材料是 `evidence_paths`** —— 任务收尾时记下的「动过的路径」。
        这就是为什么当初要让 episode 带上资源：没有它，习惯无从谈起。
        """
        if self.memory is None:
            return []
        try:
            events = self.memory.list_events(limit=100)
        except Exception:  # noqa: BLE001
            logger.debug("读事件失败", exc_info=True)
            return []

        # 只看任务类 episode，且要跳过 task_id（它是第一个，不是「动过的路径」）
        counter: Counter[str] = Counter()
        for event in events:
            if getattr(event, "page_category", "") != "桌面任务":
                continue
            for path in (event.evidence_paths or [])[1:]:
                if path and len(path) > 3:
                    counter[path] += 1

        out: list[Suggestion] = []
        for path, count in counter.most_common(3):
            if count < _HABIT_MIN_OCCURRENCES:
                continue
            out.append(Suggestion(
                kind="habit",
                what=f"整理一下 {path}",
                why=f"你在这里动过 {count} 次，像是常打交道的目录",
                confidence=min(0.5 + count * 0.05, 0.7),
                # 习惯类建议是「起个话头」，交给规划器正常拆解
                action={"kind": "run", "goal": f"整理一下 {path}"},
            ))
        return out
