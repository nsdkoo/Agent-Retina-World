"""交互式提问：把「需要用户拍板」的场景变成可点选项。

对标 block/goose 的智能批准（低风险自动放行、需要决策的显式标记待确认）与
Cursor / Claude 的 clarifying question：Agent 不替用户猜，把候选摆出来让用户选。

约定：options 是给用户看的文案，用户点选后原样作为下一条指令回到意图层，
所以文案本身要能被 parse_intent 认出来（例如「按类型归档」「确认归档」）。
"""

from __future__ import annotations

import re

from screen_agent.tools.base import ActionResult

_MAX_OPTIONS = 4
_MAX_LABEL = 24
_SPLIT_RE = re.compile(r"[|｜、,，;；\n]+")


def ask_options(question: str, options: str = "") -> ActionResult:
    """向用户提问并给出候选；options 用竖线或顿号分隔（最多 4 个）。"""
    text = (question or "").strip()
    if not text:
        return ActionResult(success=False, message="我还没想好问什么")
    labels: list[str] = []
    for item in _SPLIT_RE.split(options or ""):
        label = item.strip().strip("「」\"'")
        if not label or label in labels:
            continue
        labels.append(label[:_MAX_LABEL])
        if len(labels) >= _MAX_OPTIONS:
            break
    return ActionResult(
        success=True,
        message=text,
        detail={"ask": True},
        options=labels or None,
    )
