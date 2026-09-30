"""后果预演：动手前先在沙盘里算一遍。

## 为什么要有这一层

传统桌面 agent 是**纯反应式**的：看到 → 点下去 → 发现错了。
而桌面场景最痛的地方恰恰是——**很多操作不可逆**。
关掉没保存的文档、删了文件、发出去了消息，点下去就收不回。

人类不会这样干活。人点「删除」之前会想一下「这会删掉什么、删了还能不能找回来」。
这个模块就是让 agent 也先想一下。

## 为什么只做文本级（不训图像模型）

对标 ICLR 2026 的 CUWM（Computer-Using World Model，arXiv 2602.17365），
它把「预测下一个 UI 状态」拆成两阶段：文本描述变化 + 视觉渲染下一个截图。

但论文自己的实测结论有两条很关键：

1. **结构清晰度比像素保真度更重要** —— agent 受益于「下拉菜单出现了」这种高层描述，
   而不是一张完美的模拟截图
2. **多模态冲突悖论** —— 把模拟的文本描述和模拟的图像**同时**给 agent，**性能反而下降**
   （跨模态冲突 + 噪声累积）

结论：**对决策有用的部分几乎全在文本里**。训一个图像生成模型是几个数量级的成本，
换来的收益在决策上接近于零。所以这里只做文本级转移预测。

## 两级策略

- **规则版**（默认，永远可用）：按工具自报的 `effects` + `risk` + `idempotent` 推断。
  快、免费、离线、可解释。
- **LLM 版**（可选，仅在配了 chat_client 且动作高风险时启用）：把当前状态和候选动作给模型，
  让它说会发生什么。更准，但慢且贵，所以**只在真正需要时调**。

规则版打底、LLM 版增强——而不是反过来。因为**预演本身不能成为新的故障点**：
如果每次都等模型返回才能执行，那这套机制就成了负担。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 哪些工具「做错了就收不回来」。这份名单**只能是保守的**——
# 列进来的要拦，没列进来的默认也按不可逆处理（见 _reversible 的默认值）
_IRREVERSIBLE_HINTS = {
    "app.close": "关闭应用会丢掉未保存的内容，且没有回收站可恢复",
    "sys.lock": "锁屏会中断正在进行的操作",
    "input.click": "点击的后果取决于点到了什么，可能触发不可逆动作",
    "input.type_text": "输入的内容可能已被提交（表单、聊天、搜索框）",
    "input.press_key": "按键可能触发提交、删除、发送等不可逆动作",
    "shell.run": "命令的后果完全取决于命令本身，系统不设防",
}

# 高风险参数名——出现在参数里就提醒「这个值要确认」
_RISKY_PARAMS = ("force", "recursive", "overwrite", "delete", "--force", "-rf")


@dataclass
class Foresight:
    """一个动作的后果预演结果。"""

    tool: str
    goal: str
    change: str                              # 会发生什么（人话）
    reversible: bool                         # 可逆吗
    risks: list[str] = field(default_factory=list)
    confidence: float = 0.5                  # 把握有多大；规则版天然不会太高
    source: str = "rules"                    # rules / llm

    def render(self) -> str:
        """给用户看的一行话。用于挂起确认时的提示。"""
        parts = [f"这一步会：{self.change}"]
        if not self.reversible:
            parts.append("**较难撤销**")
        if self.risks:
            parts.append("；".join(self.risks))
        return "。".join(parts) + "。"

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "goal": self.goal,
            "change": self.change,
            "reversible": self.reversible,
            "risks": list(self.risks),
            "confidence": self.confidence,
            "source": self.source,
        }


def _fill(template: str, params: dict) -> str:
    """把模板里的 {参数名} 换成实际值。

    缺失的参数保留原样（显示成 `{dst}`）而不是抛错——
    预演是**辅助信息**，不能因为一个占位符没填上就把整个流程搞挂。
    """
    if not template:
        return ""
    try:
        return template.format(**{k: str(v) for k, v in (params or {}).items()})
    except (KeyError, IndexError, ValueError):
        result = template
        for key, value in (params or {}).items():
            result = result.replace("{" + key + "}", str(value))
        return result


class ForesightEngine:
    """后果预演引擎。

    `llm_predict` 是可选的外部函数：`(goal, tool, params, screen_hint) -> str`。
    只有配了它、且规则版判定为**高风险**时才会调用——把最贵的那条路留给最需要的场景。
    """

    def __init__(self, llm_predict=None) -> None:  # noqa: ANN001
        self.llm_predict = llm_predict

    def predict(
        self,
        step,                      # noqa: ANN001 - PlanStep
        spec,                      # noqa: ANN001 - ToolSpec
        screen_hint: str = "",
    ) -> Foresight:
        """给一个步骤做后果预演。"""
        tool = getattr(step, "tool", "") or ""
        goal = getattr(step, "goal", "") or ""
        params = dict(getattr(step, "params", {}) or {})

        change, reversible, risks = self._by_rules(tool, params, spec)

        if self.llm_predict is not None and not reversible:
            # 只在「可能收不回来」的时候才花这次模型调用
            refined = self._try_llm(goal, tool, params, screen_hint)
            if refined:
                change, confidence, source = refined, 0.75, "llm"
            else:
                confidence, source = 0.6, "rules"
        else:
            confidence, source = (0.7 if reversible else 0.55), "rules"

        return Foresight(
            tool=tool, goal=goal, change=change,
            reversible=reversible, risks=risks,
            confidence=confidence, source=source,
        )

    def _by_rules(self, tool: str, params: dict, spec) -> tuple[str, bool, list[str]]:  # noqa: ANN001
        """规则版推断。三条依据：工具自报的 effects、风险等级、幂等性。"""
        risks: list[str] = []

        # ① 工具自己声明的后果描述最准，优先用
        declared = getattr(spec, "effects", "") or ""
        if declared:
            change = _fill(declared, params)
        else:
            # ② 没声明就用 描述 + 关键参数 拼一句
            desc = getattr(spec, "description", "") or tool
            key_params = "、".join(f"{k}={v}" for k, v in list(params.items())[:3])
            change = f"{desc}（{key_params}）" if key_params else desc

        # ③ 可逆性判断，优先级从高到低：
        #    工具显式声明 > 只读/幂等（重跑无害）> 默认按不可逆
        #
        #    **默认不可逆是刻意的**：误导用户以为安全，比多提示一次危险得多。
        #    但反过来也要给「确实可撤销」的工具留出口——否则预演会把
        #    files.move（有 undo）、files.delete（走回收站）这类误报成危险动作，
        #    提示一多用户就疲劳了，真正危险的反被忽略。
        declared = getattr(spec, "reversible", None)
        if declared is not None:
            reversible = bool(declared)
        else:
            risk = getattr(spec, "risk", None)
            idempotent = bool(getattr(spec, "idempotent", False))
            reversible = bool(risk is not None and getattr(risk, "value", 9) == 0) or idempotent

        # 已知没有回收路径的工具，无论声明什么一律按不可逆
        if tool in _IRREVERSIBLE_HINTS:
            reversible = False
            risks.append(_IRREVERSIBLE_HINTS[tool])
        elif not reversible:
            risks.append("这一步没有撤销路径")

        # ④ 危险参数额外提醒。用**子串匹配**而不是精确相等——
        #    危险标记几乎总是长在别的字符串里（`rm -rf /tmp/x` 里含 `-rf`）
        for key, value in params.items():
            text = f"{key} {value}".lower()
            if any(hint in text for hint in _RISKY_PARAMS):
                risks.append(f"参数含强制/递归类标记（{key}），影响范围可能超出预期")
                break

        return change, reversible, risks

    def _try_llm(self, goal: str, tool: str, params: dict, screen_hint: str) -> str:
        """调模型补一个更准的后果描述。失败就当没有——预演不能成为新的故障点。"""
        try:
            hint = f"当前屏幕：{screen_hint}\n" if screen_hint else ""
            text = self.llm_predict(
                f"{hint}用户目标：{goal}\n"
                f"准备执行：{tool}({params})\n\n"
                f"用一句话说清这一步执行后会发生什么变化（不要解释、不要建议，只说后果）："
            )
            text = (text or "").strip().splitlines()[0] if text else ""
            return text[:120]
        except Exception:  # noqa: BLE001
            logger.debug("后果预演调模型失败", exc_info=True)
            return ""
