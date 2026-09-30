"""工具参数校验的测试。

改这个的起因：`registry.run` 原先没有任何参数校验，类型错会被 `except Exception`
兜成一句含糊的「执行失败：…」。**错误恢复机制没法从这句话里分出「参数写错了」
和「网络抖了」**——前者该重规划，后者该重试，混在一起就只能一律不救。

所以要保证：
1. 参数问题在 handler 之前就被拦下，且带 `error_kind="invalid_params"`
2. 常见类型错（"3" 当整数传）能做安全纠正，而不是直接失败
3. **没有 schema 的工具行为不变**——这是增量落地，不该一次改完所有工具
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.tools.base import ActionResult
from screen_agent.tools.registry import RiskLevel, ToolRegistry, ToolSpec


def _ok(**params) -> ActionResult:
    return ActionResult(success=True, message=f"收到 {params}")


class ValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.spec = ToolSpec(
            "demo.do", "演示工具", _ok, RiskLevel.SAFE,
            params_doc={"path": "路径", "steps": "步数", "force": "是否强制"},
            schema={
                "path": {"type": "string", "required": True},
                "steps": {"type": "integer"},
                "force": {"type": "boolean"},
            },
        )

    def test_rejects_unknown_key(self) -> None:
        ok, why, _ = self.registry.validate(self.spec, {"path": "a", "bogus": 1})
        self.assertFalse(ok)
        self.assertIn("bogus", why)

    def test_rejects_missing_required(self) -> None:
        ok, why, _ = self.registry.validate(self.spec, {"steps": 1})
        self.assertFalse(ok)
        self.assertIn("path", why)

    def test_optional_keys_may_be_absent(self) -> None:
        ok, _, cleaned = self.registry.validate(self.spec, {"path": "a"})
        self.assertTrue(ok)
        self.assertEqual(cleaned, {"path": "a"})

    def test_coerces_integer_string(self) -> None:
        ok, _, cleaned = self.registry.validate(self.spec, {"path": "a", "steps": "3"})
        self.assertTrue(ok)
        self.assertEqual(cleaned["steps"], 3)
        self.assertIsInstance(cleaned["steps"], int)

    def test_coerces_boolean_string(self) -> None:
        for text, expect in (("true", True), ("false", False), ("是", True), ("否", False)):
            with self.subTest(text=text):
                ok, _, cleaned = self.registry.validate(
                    self.spec, {"path": "a", "force": text}
                )
                self.assertTrue(ok)
                self.assertIs(cleaned["force"], expect)

    def test_rejects_uncoercible_type(self) -> None:
        ok, why, _ = self.registry.validate(self.spec, {"path": "a", "steps": "三步"})
        self.assertFalse(ok)
        self.assertIn("steps", why)

    def test_passes_through_correct_types(self) -> None:
        ok, _, cleaned = self.registry.validate(
            self.spec, {"path": "a", "steps": 5, "force": True}
        )
        self.assertTrue(ok)
        self.assertEqual(cleaned, {"path": "a", "steps": 5, "force": True})

    def test_no_schema_is_permissive(self) -> None:
        """没填 schema 的工具必须完全按现状工作——否则所有存量工具都要一起改。"""
        bare = ToolSpec("demo.bare", "无 schema", _ok, RiskLevel.SAFE,
                        params_doc={"anything": "随便"})
        ok, _, cleaned = self.registry.validate(bare, {"anything": "x", "extra": 1})
        self.assertTrue(ok)
        self.assertEqual(cleaned, {"anything": "x", "extra": 1})


class RunValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(ToolSpec(
            "demo.do", "演示工具", _ok, RiskLevel.SAFE,
            schema={"path": {"type": "string", "required": True},
                    "steps": {"type": "integer"}},
        ))

    def test_invalid_params_has_its_own_kind(self) -> None:
        """关键：参数错要有独立的 error_kind，不能混进「执行失败」。"""
        result = self.registry.run("demo.do", steps=1)
        self.assertFalse(result.success)
        self.assertEqual(result.error_kind, "invalid_params")
        self.assertNotIn("执行失败", result.message)

    def test_handler_not_called_on_invalid_params(self) -> None:
        called: list[dict] = []

        def _spy(**params) -> ActionResult:
            called.append(params)
            return ActionResult(success=True, message="ok")

        registry = ToolRegistry()
        registry.register(ToolSpec(
            "demo.spy", "记录是否被调用", _spy, RiskLevel.SAFE,
            schema={"path": {"type": "string", "required": True}},
        ))
        registry.run("demo.spy", bogus="x")
        self.assertEqual(called, [], "handler 在参数校验失败时仍被调用了")

    def test_coerced_params_reach_handler(self) -> None:
        result = self.registry.run("demo.do", path="a", steps="7")
        self.assertTrue(result.success)
        self.assertIn("'steps': 7", result.message)

    def test_unknown_tool_is_fatal(self) -> None:
        result = self.registry.run("no.such.tool")
        self.assertFalse(result.success)
        self.assertEqual(result.error_kind, "fatal")

    def test_no_schema_tool_runs_unchanged(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolSpec("demo.bare", "无 schema", _ok, RiskLevel.SAFE,
                                   params_doc={"a": "随便"}))
        self.assertTrue(registry.run("demo.bare", a="1", b=2).success)


class IdempotentFlagTests(unittest.TestCase):
    def test_default_is_false(self) -> None:
        """默认必须保守：写操作不标幂等，宁可少救也不重试出副作用。"""
        spec = ToolSpec("demo.x", "x", _ok)
        self.assertFalse(spec.idempotent)

    def test_read_tools_marked_idempotent(self) -> None:
        from screen_agent.tools.registry_setup import build_default_registry

        registry = build_default_registry()
        for name in ("files.list", "files.find", "files.read", "files.grep",
                     "files.glob", "app.list"):
            with self.subTest(tool=name):
                spec = registry.get(name)
                if spec is not None:
                    self.assertTrue(spec.idempotent, f"{name} 是纯读工具，应当标幂等")

    def test_write_tools_not_marked(self) -> None:
        from screen_agent.tools.registry_setup import build_default_registry

        registry = build_default_registry()
        for name in ("files.move", "files.copy", "files.write", "files.delete",
                     "files.mkdir", "shell.run"):
            with self.subTest(tool=name):
                spec = registry.get(name)
                if spec is not None:
                    self.assertFalse(spec.idempotent, f"{name} 重跑有副作用，不能标幂等")


class OpenAISchemaTests(unittest.TestCase):
    def test_required_only_lists_mandatory(self) -> None:
        """有 schema 时 required 要准——旧实现把「所有」参数都当必填，
        结果 LLM 以为 path 这类可选参数也必须给。"""
        registry = ToolRegistry()
        registry.register(ToolSpec(
            "demo.do", "演示", _ok, RiskLevel.SAFE,
            schema={"path": {"type": "string", "required": True},
                    "steps": {"type": "integer"}},
        ))
        tool = registry.list_openai_tools()[0]
        params = tool["function"]["parameters"]
        self.assertEqual(params["required"], ["path"])
        self.assertEqual(params["properties"]["steps"]["type"], "integer")

    def test_without_schema_falls_back(self) -> None:
        """没 schema 时保持旧行为（全 string、全必填），避免影响存量工具。"""
        registry = ToolRegistry()
        registry.register(ToolSpec("demo.bare", "无 schema", _ok, RiskLevel.SAFE,
                                   params_doc={"a": "说明"}))
        params = registry.list_openai_tools()[0]["function"]["parameters"]
        self.assertEqual(params["properties"]["a"]["type"], "string")
        self.assertEqual(params["required"], ["a"])


if __name__ == "__main__":
    unittest.main()
