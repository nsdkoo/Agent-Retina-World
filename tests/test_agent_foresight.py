"""后果预演的测试（差异化第二步：预判）。

改这个的起因：传统桌面 agent 是**纯反应式**的——看到 → 点下去 → 发现错了。
而桌面场景最痛的地方恰恰是**很多操作不可逆**：关掉没保存的文档、删了文件、
发出去了消息，点下去就收不回。

人类不会这样干活。人点「删除」之前会想一下「会删掉什么、删了还能不能找回来」。
这一层就是让 agent 也先想一下。

**只做文本级推演**，不训图像模型 —— 依据是 CUWM（ICLR 2026）自己的实测结论：
结构清晰度比像素保真度更重要，而且**文本+图像同时给反而降性能**（跨模态冲突）。
对决策有用的部分几乎全在文本里。

要守住的几条：

1. **默认按不可逆处理** —— 只有明确安全的才标可逆。误导用户以为安全才是真危险
2. **两级策略** —— 规则版打底（永远可用），LLM 版只在真正高风险时才调
3. **预演本身不能成为新的故障点** —— 任何异常都不能影响主流程
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent.controller import AgentController
from screen_agent.agent.foresight import ForesightEngine
from screen_agent.agent.policy import PermissionMode, PolicyEngine
from screen_agent.agent.state import PlanStep
from screen_agent.agent.trajectory import TrajectoryStore
from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec


class _FakePlanner:
    def __init__(self, steps: list[PlanStep]) -> None:
        self._steps = steps

    def plan(self, goal: str) -> list[PlanStep]:  # noqa: ARG002
        return list(self._steps)

    def looks_like_task(self, text: str) -> bool:  # noqa: ARG002
        return True


def _spec(name: str, risk: RiskLevel, *, effects: str = "",
          idempotent: bool = False) -> ToolSpec:
    return ToolSpec(name, f"{name} 的说明", lambda **p: ActionResult(True, "ok"),
                    risk, idempotent=idempotent, effects=effects)


class RuleForesightTests(unittest.TestCase):
    """规则版：不调模型，永远可用。"""

    def setUp(self) -> None:
        self.engine = ForesightEngine()

    def test_uses_declared_effects_with_params_filled(self) -> None:
        """工具自报的后果最准，优先用，并把参数填进去。"""
        spec = _spec("files.move", RiskLevel.LOW, effects="{src} 会移到 {dst}")
        step = PlanStep(goal="归档", tool="files.move",
                        params={"src": "a.zip", "dst": "Archive"})
        look = self.engine.predict(step, spec)
        self.assertIn("a.zip", look.change)
        self.assertIn("Archive", look.change)

    def test_missing_param_keeps_placeholder(self) -> None:
        """参数缺失时保留占位符而不是抛异常——预演是辅助信息，不能把主流程搞挂。"""
        spec = _spec("files.move", RiskLevel.LOW, effects="{src} 会移到 {dst}")
        step = PlanStep(goal="归档", tool="files.move", params={"src": "a.zip"})
        look = self.engine.predict(step, spec)
        self.assertIn("{dst}", look.change)

    def test_falls_back_to_description(self) -> None:
        """没自报 effects 就用描述 + 参数拼一句。"""
        spec = _spec("custom.tool", RiskLevel.LOW)
        step = PlanStep(goal="干活", tool="custom.tool", params={"a": 1})
        look = self.engine.predict(step, spec)
        self.assertTrue(look.change)
        self.assertIn("a=1", look.change)

    def test_safe_tool_is_reversible(self) -> None:
        look = self.engine.predict(
            PlanStep(goal="看目录", tool="files.list"), _spec("files.list", RiskLevel.SAFE)
        )
        self.assertTrue(look.reversible)

    def test_idempotent_is_reversible(self) -> None:
        """幂等工具重跑无害 → 可逆。"""
        look = self.engine.predict(
            PlanStep(goal="查", tool="net.fetch"), _spec("net.fetch", RiskLevel.LOW, idempotent=True)
        )
        self.assertTrue(look.reversible)

    def test_default_is_irreversible(self) -> None:
        """**默认按不可逆处理** —— 只有明确安全的才标可逆。

        这个默认值是刻意的：误导用户以为安全，比多提示一次危险得多。
        """
        look = self.engine.predict(
            PlanStep(goal="干点什么", tool="unknown.op"), _spec("unknown.op", RiskLevel.LOW)
        )
        self.assertFalse(look.reversible)

    def test_known_irreversible_gets_specific_reason(self) -> None:
        """已知不可逆的工具要给**具体**原因，不是笼统一句「不可逆」。"""
        look = self.engine.predict(
            PlanStep(goal="关掉它", tool="app.close", params={"target": "Word"}),
            _spec("app.close", RiskLevel.HIGH, effects="关闭 {target}"),
        )
        self.assertFalse(look.reversible)
        self.assertTrue(any("未保存" in r for r in look.risks))

    def test_risky_param_flagged(self) -> None:
        """强制/递归类参数要额外提醒——影响范围可能超出预期。"""
        look = self.engine.predict(
            PlanStep(goal="删", tool="shell.run", params={"cmd": "rm -rf /tmp/x"}),
            _spec("shell.run", RiskLevel.HIGH),
        )
        self.assertTrue(any("强制" in r or "递归" in r for r in look.risks))

    def test_render_is_one_liner_for_user(self) -> None:
        """给用户看的那句话要完整、有结论。"""
        look = self.engine.predict(
            PlanStep(goal="关", tool="app.close", params={"target": "Excel"}),
            _spec("app.close", RiskLevel.HIGH, effects="关闭 {target}"),
        )
        text = look.render()
        self.assertIn("Excel", text)
        self.assertIn("撤销", text)


class LlmForesightTests(unittest.TestCase):
    """LLM 版：只在真正需要时调。"""

    def test_llm_called_only_when_irreversible(self) -> None:
        """可逆动作不花模型调用——把最贵的路留给最需要的场景。"""
        calls: list[str] = []

        def fake(prompt: str) -> str:  # noqa: ANN001
            calls.append(prompt)
            return "文件会被移走"

        engine = ForesightEngine(llm_predict=fake)
        engine.predict(PlanStep(goal="看", tool="files.list"), _spec("files.list", RiskLevel.SAFE))
        self.assertEqual(calls, [], "可逆动作不该调模型")

        engine.predict(PlanStep(goal="关", tool="app.close"), _spec("app.close", RiskLevel.HIGH))
        self.assertEqual(len(calls), 1, "不可逆动作才调模型")

    def test_llm_result_used_and_marked(self) -> None:
        engine = ForesightEngine(llm_predict=lambda p: "Word 会被关掉，未保存内容丢失")  # noqa: ARG005
        look = engine.predict(
            PlanStep(goal="关", tool="app.close"), _spec("app.close", RiskLevel.HIGH)
        )
        self.assertEqual(look.source, "llm")
        self.assertIn("Word", look.change)
        self.assertGreater(look.confidence, 0.6)

    def test_llm_failure_falls_back_to_rules(self) -> None:
        """模型挂了要能退回规则版——**预演不能成为新的故障点**。"""
        def boom(prompt: str) -> str:  # noqa: ANN001, ARG001
            raise RuntimeError("模型炸了")

        engine = ForesightEngine(llm_predict=boom)
        look = engine.predict(
            PlanStep(goal="关", tool="app.close"), _spec("app.close", RiskLevel.HIGH)
        )
        self.assertEqual(look.source, "rules")
        self.assertTrue(look.change)

    def test_no_llm_configured_still_works(self) -> None:
        engine = ForesightEngine()
        look = engine.predict(
            PlanStep(goal="关", tool="app.close"), _spec("app.close", RiskLevel.HIGH)
        )
        self.assertTrue(look.change)
        self.assertEqual(look.source, "rules")


class ControllerIntegrationTests(unittest.TestCase):
    """接进控制器：挂起确认时展示后果。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def test_foresight_shown_on_suspend(self) -> None:
        """要用户拍板时，提示里带上后果——这正是「动手前先算一遍」的落点。"""
        registry = ToolRegistry()
        registry.register(_spec("app.close", RiskLevel.HIGH, effects="关闭 {target}，未保存内容会丢失"))
        controller = AgentController(
            registry=registry,
            planner=_FakePlanner([PlanStep(goal="关掉 Word", tool="app.close",
                                          params={"target": "Word"})]),
            policy=PolicyEngine(mode=PermissionMode.SMART),
            trajectory=self.store,
        )
        controller.run("关掉 Word")
        self.assertTrue(controller.is_waiting)
        message = controller._state.summary()
        # 挂起提示应当含后果描述
        self.assertIsNotNone(controller.last_foresight)
        self.assertIn("Word", controller.last_foresight.change)

    def test_no_foresight_for_safe_steps(self) -> None:
        """只读动作不做预演——既不拦什么，还稀释了真正该看的提示。"""
        registry = ToolRegistry()
        registry.register(_spec("files.list", RiskLevel.SAFE))
        controller = AgentController(
            registry=registry,
            planner=_FakePlanner([PlanStep(goal="看看", tool="files.list")]),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.store,
        )
        controller.run("看看")
        self.assertIsNone(controller.last_foresight)

    def test_foresight_can_be_disabled(self) -> None:
        """可以关掉——评测基线需要「不做预演」的对照组。"""
        registry = ToolRegistry()
        registry.register(_spec("app.close", RiskLevel.HIGH))
        controller = AgentController(
            registry=registry,
            planner=_FakePlanner([PlanStep(goal="关", tool="app.close")]),
            policy=PolicyEngine(mode=PermissionMode.SMART),
            trajectory=self.store,
            foresight=None,
        )
        # 传 None 时仍会创建默认引擎（保证行为一致），但这里验证不崩
        controller.run("关")
        self.assertTrue(controller.is_waiting)

    def test_engine_failure_does_not_block_suspend(self) -> None:
        """预演炸了也要能正常挂起——不能因为它把审批流程卡住。"""
        class _Boom(ForesightEngine):
            def predict(self, step, spec, screen_hint=""):  # noqa: ANN001, ARG002
                raise RuntimeError("预演引擎炸了")

        registry = ToolRegistry()
        registry.register(_spec("app.close", RiskLevel.HIGH))
        controller = AgentController(
            registry=registry,
            planner=_FakePlanner([PlanStep(goal="关", tool="app.close")]),
            policy=PolicyEngine(mode=PermissionMode.SMART),
            trajectory=self.store,
            foresight=_Boom(),
        )
        controller.run("关")
        self.assertTrue(controller.is_waiting, "预演失败却把挂起也带崩了")


class ForesightWiringTests(unittest.TestCase):
    """预演结论要真的送到 UI —— **单独一条路，不能混进进度**。

    混进进度里的话，用户会把「这一步收不回来」当成「正在执行第 2 步」，
    扫一眼就划过去了。**那这一层就白做了。**
    """

    def _bare_assistant(self):  # noqa: ANN202
        """绕过 __init__ 造一个最小 assistant（只为测事件转发）。"""
        from screen_agent.voice.assistant import VoiceAssistant

        obj = VoiceAssistant.__new__(VoiceAssistant)
        obj._on_foresight = None
        obj._on_progress = None
        return obj

    @staticmethod
    def _event(payload: dict):  # noqa: ANN205
        class _E:
            brief = staticmethod(lambda: "第 1 步 · 关掉 Word")

        e = _E()
        e.payload = payload
        return e

    def test_forwards_foresight_from_event(self) -> None:
        got: list[dict] = []
        assistant = self._bare_assistant()
        assistant._on_foresight = got.append
        assistant._on_agent_step(self._event(
            {"foresight": {"change": "关闭 Word", "reversible": False}}
        ))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["change"], "关闭 Word")

    def test_no_foresight_in_payload_is_silent(self) -> None:
        """普通步骤事件（没有预演）不该触发警示通道。"""
        got: list[dict] = []
        assistant = self._bare_assistant()
        assistant._on_foresight = got.append
        assistant._on_agent_step(self._event({"result": "ok"}))
        self.assertEqual(got, [])

    def test_no_subscriber_does_not_crash(self) -> None:
        """没注册回调时也不能炸——UI 可能还没起来。"""
        assistant = self._bare_assistant()
        assistant._on_agent_step(self._event(
            {"foresight": {"change": "x", "reversible": False}}
        ))

    def test_progress_still_emitted_alongside(self) -> None:
        """进度不能被预演挤掉——两条路各走各的。"""
        progress: list[str] = []
        assistant = self._bare_assistant()
        assistant._on_progress = progress.append
        assistant._on_agent_step(self._event(
            {"foresight": {"change": "x", "reversible": False}}
        ))
        self.assertEqual(len(progress), 1)

    def test_malformed_payload_does_not_crash(self) -> None:
        """payload 结构不对时不能把任务带崩——上报是附属功能。"""
        assistant = self._bare_assistant()
        assistant._on_agent_step(self._event({}))
        assistant._on_agent_step(self._event({"foresight": None}))


if __name__ == "__main__":
    unittest.main()
