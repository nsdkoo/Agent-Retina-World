"""trace / 审计回放的测试。

改这个的起因：`events` 表**只写不读**、也没有 span 概念——
花了写盘的钱，却没换来任何排障能力。出问题时你没法回答
「这次任务每一步调了什么、花了多久、哪一步被策略拦了」。

对标 OTel GenAI semconv，但**不引 SDK**（单机桌面助手，为自研 harness 拉一套
collector/exporter 是过度工程）。字段名照它的约定起，将来想导出只差一个 exporter。

三条要守住的：
1. **层级正确**：1 task + N function + ≥N guardrail，父子链能对上
2. **内容进 events 不进 attributes**——attributes 会被全量索引，塞 PII 进去等于摊在监控里
3. **`trace_content=False` 时真的不留内容**（隐私开关得是真开关）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.agent.controller import AgentController
from screen_agent.agent.policy import PermissionMode, PolicyEngine
from screen_agent.agent.state import PlanStep
from screen_agent.agent.trace import SpanRecorder
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


def _tool(name: str, risk: RiskLevel = RiskLevel.LOW) -> ToolSpec:
    return ToolSpec(name, f"演示工具 {name}",
                    lambda **params: ActionResult(success=True, message=f"{name} 完成 {params}"),
                    risk)


class TraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def _run(self, steps: list[PlanStep], specs: list[ToolSpec],
             trace_content: bool = True) -> str:
        registry = ToolRegistry()
        for spec in specs:
            registry.register(spec)
        controller = AgentController(
            registry=registry, planner=_FakePlanner(steps),
            policy=PolicyEngine(mode=PermissionMode.AUTO),
            trajectory=self.store, trace_content=trace_content,
        )
        controller.run("演示任务")
        return controller.state.task_id

    def test_span_hierarchy(self) -> None:
        """1 个 task span + 每步 1 个 function span + 每步 ≥1 个 guardrail span。"""
        steps = [PlanStep(goal="第一步", tool="demo.a"), PlanStep(goal="第二步", tool="demo.b")]
        task_id = self._run(steps, [_tool("demo.a"), _tool("demo.b")])

        spans = self.store.read_spans(task_id)
        by_kind: dict[str, list[dict]] = {}
        for span in spans:
            by_kind.setdefault(span["kind"], []).append(span)

        self.assertEqual(len(by_kind.get("task", [])), 1, "应当只有一条任务级 span")
        self.assertEqual(len(by_kind.get("function", [])), 2, "每步一个工具 span")
        self.assertGreaterEqual(len(by_kind.get("guardrail", [])), 2, "每步都该过权限门并留痕")

    def test_spans_are_parented_to_task(self) -> None:
        task_id = self._run([PlanStep(goal="干活", tool="demo.a")], [_tool("demo.a")])
        spans = self.store.read_spans(task_id)
        task = next(s for s in spans if s["kind"] == "task")
        children = [s for s in spans if s["kind"] != "task"]
        self.assertTrue(children)
        for child in children:
            self.assertEqual(child["parent_span_id"], task["span_id"])
            self.assertEqual(child["trace_id"], task_id)

    def test_tool_span_uses_otel_attribute_names(self) -> None:
        task_id = self._run([PlanStep(goal="干活", tool="demo.a")], [_tool("demo.a")])
        function = next(s for s in self.store.read_spans(task_id) if s["kind"] == "function")
        self.assertEqual(function["attributes"]["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(function["attributes"]["gen_ai.tool.name"], "demo.a")
        self.assertIn("step.index", function["attributes"])

    def test_task_span_marks_agent_invocation(self) -> None:
        task_id = self._run([PlanStep(goal="干活", tool="demo.a")], [_tool("demo.a")])
        task = next(s for s in self.store.read_spans(task_id) if s["kind"] == "task")
        self.assertEqual(task["attributes"]["gen_ai.operation.name"], "invoke_agent")

    def test_content_goes_to_events_not_attributes(self) -> None:
        """内容（工具参数、输出）必须在 events 里——attributes 会被全量索引。"""
        task_id = self._run(
            [PlanStep(goal="干活", tool="demo.a", params={"path": "/tmp/秘密文件.txt"})],
            [_tool("demo.a")],
        )
        function = next(s for s in self.store.read_spans(task_id) if s["kind"] == "function")
        self.assertNotIn("path", str(function["attributes"]))
        self.assertTrue(function["events"], "内容应当作为 span events 存在")

    def test_trace_content_off_drops_events(self) -> None:
        """隐私开关得是真开关：关掉之后库里不该有任何内容。"""
        task_id = self._run(
            [PlanStep(goal="干活", tool="demo.a", params={"path": "/tmp/秘密文件.txt"})],
            [_tool("demo.a")], trace_content=False,
        )
        spans = self.store.read_spans(task_id)
        self.assertTrue(spans, "关内容不影响 span 骨架")
        for span in spans:
            self.assertEqual(span["events"], [], f"{span['name']} 关了内容却还留着 events")
            self.assertNotIn("秘密文件", str(span["attributes"]))

    def test_guardrail_records_decision(self) -> None:
        task_id = self._run([PlanStep(goal="干活", tool="demo.a")], [_tool("demo.a")])
        guard = next(s for s in self.store.read_spans(task_id) if s["kind"] == "guardrail")
        self.assertEqual(guard["attributes"]["gen_ai.operation.name"], "guardrail")
        self.assertIn("policy.decision", guard["attributes"])
        self.assertIn("policy.mode", guard["attributes"])


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrajectoryStore(Path(self._tmp.name) / "tasks.db")

    def _run(self, steps: list[PlanStep], specs: list[ToolSpec]) -> str:
        registry = ToolRegistry()
        for spec in specs:
            registry.register(spec)
        controller = AgentController(
            registry=registry, planner=_FakePlanner(steps),
            policy=PolicyEngine(mode=PermissionMode.AUTO), trajectory=self.store,
        )
        controller.run("演示任务")
        return controller.state.task_id

    def test_replay_is_time_ordered(self) -> None:
        steps = [PlanStep(goal="第一步", tool="demo.a"), PlanStep(goal="第二步", tool="demo.b")]
        task_id = self._run(steps, [_tool("demo.a"), _tool("demo.b")])

        timeline = self.store.replay(task_id)
        self.assertTrue(timeline)
        times = [entry["at"] for entry in timeline]
        self.assertEqual(times, sorted(times), "回放时间线必须按时间排序")

    def test_replay_merges_spans_and_events(self) -> None:
        task_id = self._run([PlanStep(goal="干活", tool="demo.a")], [_tool("demo.a")])
        timeline = self.store.replay(task_id)
        kinds = {entry["type"] for entry in timeline}
        self.assertIn("span", kinds)
        self.assertIn("event", kinds, "事件流也该出现在回放里")

    def test_read_events_fills_the_gap(self) -> None:
        """events 表原先只写不读——这个接口把它接上了。"""
        task_id = self._run([PlanStep(goal="干活", tool="demo.a")], [_tool("demo.a")])
        events = self.store.read_events(task_id)
        self.assertTrue(events)
        self.assertTrue(all("kind" in e for e in events))

    def test_export_otel_shape(self) -> None:
        task_id = self._run([PlanStep(goal="干活", tool="demo.a")], [_tool("demo.a")])
        exported = self.store.export_otel(task_id)
        self.assertTrue(exported)
        first = exported[0]
        for key in ("traceId", "spanId", "parentSpanId", "name", "kind",
                    "startTimeUnixNano", "endTimeUnixNano", "status", "attributes", "events"):
            self.assertIn(key, first, f"导出缺少 OTLP 字段 {key}")
        self.assertEqual(first["traceId"], task_id)


class RecorderTests(unittest.TestCase):
    def test_redact_masks_sensitive_keys(self) -> None:
        recorder = SpanRecorder()
        cleaned = recorder.redact({
            "api_key": "sk-live-xxx", "token": "abc", "password": "p",
            "path": "/tmp/正常路径", "nested": {"secret": "s", "ok": 1},
        })
        self.assertEqual(cleaned["api_key"], "***")
        self.assertEqual(cleaned["token"], "***")
        self.assertEqual(cleaned["password"], "***")
        self.assertEqual(cleaned["path"], "/tmp/正常路径")
        self.assertEqual(cleaned["nested"]["secret"], "***")
        self.assertEqual(cleaned["nested"]["ok"], 1)

    def test_finish_sets_status_and_end(self) -> None:
        recorder = SpanRecorder()
        span = recorder.start("x", "function")
        self.assertIsNone(span.end_ts)
        recorder.finish(span, "error")
        self.assertEqual(span.status, "error")
        self.assertIsNotNone(span.end_ts)



if __name__ == "__main__":
    unittest.main()
