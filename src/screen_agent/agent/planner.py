"""任务规划：把一句复合指令拆成可执行步骤。

对标 Cline 的 Plan 模式——先想清楚再动手，且每一步都要能被用户看懂（带 why）。

两路并行，规则优先：

1. **规则切分**：按连接词把复合句切开，每段丢给现成的意图层试跑；
   能解析出工具的就是一步。这条路零成本、零延迟，覆盖日常大多数说法。
2. **LLM 兜底**：规则拆不动时让模型输出 JSON 步骤列表，本地强校验
   （工具必须已注册、风险级别不超阈值、步数不超上限、参数键必须存在）。

规划失败不硬撑——返回空列表，调用方退回单步对话，别把用户指令吞掉。
"""

from __future__ import annotations

import json
import logging
import re

from screen_agent.agent.state import PlanStep
from screen_agent.tools.registry import RiskLevel, ToolRegistry
from screen_agent.voice.intents import IntentType, parse_intent

logger = logging.getLogger(__name__)

# 复合句的连接词：切在这上面，每段各自解析
_CLAUSE_SPLIT = re.compile(r"然后|接着|之后|再帮|再|并且|并|同时|顺便|，|,|；|;")

# 动作动词：一句话里出现两个以上不同动作，才值得走规划
_ACTION_VERBS = (
    "整理", "归类", "新建", "创建", "移动", "挪", "复制", "拷贝", "重命名", "改名",
    "删除", "删掉", "打开", "关闭", "截图", "复制到", "发", "搜索", "查找",
)

_MAX_GOAL_CHARS = 300


def split_clauses(text: str) -> list[str]:
    """按连接词切开复合句，去掉空段与过短噪声段。"""
    parts = [p.strip(" 　、。") for p in _CLAUSE_SPLIT.split(text or "")]
    return [p for p in parts if len(p) >= 2]


class Planner:
    """规划器：规则优先，LLM 兜底，结果一律要过本地校验。"""

    def __init__(
        self,
        registry: ToolRegistry,
        chat_client=None,  # noqa: ANN001 - 鸭子类型，只想用 complete()
        app_aliases: dict[str, str] | None = None,
        url_aliases: dict[str, str] | None = None,
        max_steps: int = 5,
        max_risk: RiskLevel = RiskLevel.LOW,
    ) -> None:
        self.registry = registry
        self.chat_client = chat_client
        self.app_aliases = app_aliases or {}
        self.url_aliases = url_aliases or {}
        self.max_steps = max_steps
        self.max_risk = max_risk

    # ---- 要不要走规划 ----

    def looks_like_task(self, text: str) -> bool:
        """判断这句话是否值得走规划。

        顺序很关键：**先看能不能拆成多步**。单步规则的正则相当贪婪，
        「新建文件夹 素材 然后 把报告.pdf 移动到 素材」会被整体吃成一个 mkdir，
        拿单步解析结果去判断就会漏掉真正的复合指令。
        """
        text = (text or "").strip()
        if not (6 <= len(text) <= _MAX_GOAL_CHARS):
            return False
        if len(self._rule_plan(text)) >= 2:
            return True
        # 单句：规则层接住了就走快路径，没接住且模型可用才交给规划器
        if parse_intent(text, self.app_aliases, self.url_aliases).type is not IntentType.CHAT:
            return False
        return self.chat_client is not None and hasattr(self.chat_client, "complete")

    # ---- 规划 ----

    def plan(self, goal: str) -> list[PlanStep]:
        steps = self._rule_plan(goal)
        if steps:
            return steps
        return self._llm_plan(goal)

    def _rule_plan(self, goal: str) -> list[PlanStep]:
        """把每个子句交给意图层；能解析出工具的才算一步。"""
        clauses = split_clauses(goal)
        if len(clauses) < 2:
            return []
        steps: list[PlanStep] = []
        for clause in clauses[: self.max_steps]:
            intent = parse_intent(clause, self.app_aliases, self.url_aliases)
            if not intent.tool:
                return []  # 有一段接不住就整体放弃，交给 LLM 兜底
            spec = self.registry.get(intent.tool)
            if spec is None or spec.risk > self.max_risk:
                return []
            steps.append(PlanStep(
                goal=clause,
                tool=intent.tool,
                params=dict(intent.params),
                why=f"对应你说的「{clause}」",
            ))
        return steps if len(steps) >= 2 else []

    def _llm_plan(self, goal: str) -> list[PlanStep]:
        if self.chat_client is None or not hasattr(self.chat_client, "complete"):
            return []
        try:
            raw = self.chat_client.complete(
                [{"role": "user", "content": self._prompt(goal)}],
                system="你是任务规划器，只输出 JSON，不要解释。",
            )
        except Exception:  # noqa: BLE001 - 规划失败退回单步对话
            logger.debug("LLM 规划失败", exc_info=True)
            return []
        return self._validate(self._extract_json(raw))

    def _prompt(self, goal: str) -> str:
        lines = []
        for spec in self.registry.list_tools(max_risk=self.max_risk):
            params = "、".join(spec.params_doc.keys()) or "无参数"
            lines.append(f"- {spec.name}（{spec.description}；参数：{params}）")
        catalogue = "\n".join(lines)
        return (
            f"把用户目标拆成不超过 {self.max_steps} 个可执行步骤。\n"
            f"可用工具：\n{catalogue}\n\n"
            f"只输出 JSON 数组，每项形如 "
            f'{{"goal": "这一步在做什么（人话）", "tool": "工具名", '
            f'"params": {{"参数名": "值"}}, "why": "为什么要这步"}}。\n'
            f"没有合适工具就不要硬凑，输出 []。参数名必须来自上面列出的参数。\n\n"
            f"用户目标：{goal}"
        )

    @staticmethod
    def _extract_json(raw: str) -> list:
        """模型爱把 JSON 包在 ```json 里或加前后话，抠出来。"""
        text = (raw or "").strip()
        fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if fence:
            text = fence.group(1).strip()
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def _validate(self, items: list) -> list[PlanStep]:
        """本地强校验：注册表里没有的工具、超出风险阈值的、参数键不认识的，一律丢。"""
        steps: list[PlanStep] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("tool") or "").strip()
            spec = self.registry.get(name)
            if spec is None or spec.risk > self.max_risk:
                continue
            params = item.get("params")
            if not isinstance(params, dict):
                params = {}
            allowed = set(spec.params_doc.keys())
            clean = {k: v for k, v in params.items() if k in allowed}
            steps.append(PlanStep(
                goal=str(item.get("goal") or spec.description)[:80],
                tool=name,
                params=clean,
                why=str(item.get("why") or "")[:80],
            ))
            if len(steps) >= self.max_steps:
                break
        return steps if len(steps) >= 2 else []
