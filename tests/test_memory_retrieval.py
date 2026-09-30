"""画像层检索的测试。

这组测试的核心目的：**证明「检索」测的真是检索**。

原实现里 `_trial_retrieve` 是一句 `return self._trial_extract(case)`，
而 `run()` 调的又是 `_trial_extract` —— 于是「检索召回」测的其实是抽取能力，
真正的检索一天都没被测过。加上 `SEED_MEMORY` 里只有 1 条 retrieve 样本、
且答案就写在助手回复里，抽取链路必然命中，召回永远是「1/1 = 100%」。

所以这里用两条硬断言守住：
1. 检索路径**不许**调用抽取
2. 库里没灌东西时**必须**召回不到（否则说明它没在查库）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.eval.memory import SEED_MEMORY, MemoryCase, MemoryEvaluator
from screen_agent.memory.fact_retriever import FactRetriever
from screen_agent.memory.store import MemoryStoreV2


class RetrievalIsRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _store(self) -> MemoryStoreV2:
        return MemoryStoreV2(Path(self._tmp.name) / f"{len(list(Path(self._tmp.name).iterdir()))}.db")

    def _case(self, **kwargs) -> MemoryCase:  # noqa: ANN003
        base = {"case_id": "x", "kind": "retrieve"}
        base.update(kwargs)
        return MemoryCase(**base)  # type: ignore[arg-type]

    def test_retrieve_path_does_not_call_extract(self) -> None:
        """关键回归：检索不许回头调抽取。"""
        evaluator = MemoryEvaluator()
        called: list[str] = []

        def _spy(case):  # noqa: ANN001, ANN202
            called.append(case.case_id)
            return True

        evaluator._trial_extract = _spy  # type: ignore[method-assign]
        case = self._case(
            seed_facts=[{"category": "profile", "content": "用户名字是小林"}],
            query="我叫什么",
            expect_recall=["小林"],
        )
        evaluator._trial_retrieve(case)
        self.assertEqual(called, [], "检索路径调用了抽取——又会退化成假检索")

    def test_empty_store_returns_nothing(self) -> None:
        """没灌任何事实时必须召回不到，否则说明它压根没在查库。"""
        evaluator = MemoryEvaluator()
        case = self._case(query="我叫什么", expect_recall=["小林"])
        self.assertFalse(evaluator._trial_retrieve(case))

    def test_seeded_fact_is_recalled(self) -> None:
        evaluator = MemoryEvaluator()
        case = self._case(
            seed_facts=[{"category": "profile", "content": "用户名字是小林"}],
            query="我叫什么名字",
            expect_recall=["小林"],
        )
        self.assertTrue(evaluator._trial_retrieve(case))

    def test_seed_samples_are_well_formed(self) -> None:
        """内置检索样本必须带 seed_facts 与 query，否则测不了检索。"""
        retrieves = [c for c in SEED_MEMORY if c.kind == "retrieve"]
        self.assertGreaterEqual(len(retrieves), 3, "检索样本太少")
        for case in retrieves:
            self.assertTrue(case.query, f"{case.case_id} 缺 query")
            self.assertTrue(case.seed_facts, f"{case.case_id} 缺 seed_facts")
            self.assertTrue(case.expect_recall, f"{case.case_id} 缺 expect_recall")


class FactRetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MemoryStoreV2(Path(self._tmp.name) / "facts.db")

    def test_relevance_ranks_first(self) -> None:
        """相关的事实要排在前面。"""
        self.store.add_fact("project", "用户在做一个 Agent 记忆系统", confidence=0.8)
        self.store.add_fact("preference", "用户偏好浅色主题", confidence=0.8)
        hits = FactRetriever(self.store).retrieve("记忆系统", top_k=2)
        self.assertTrue(hits)
        self.assertIn("记忆系统", hits[0].fact.content)

    def test_no_query_falls_back_to_weight(self) -> None:
        """没有查询时按事实权重排——高置信度的在前。"""
        self.store.add_fact("profile", "用户的职业是前端工程师", confidence=0.95)
        self.store.add_fact("entity", "用户提过一个临时话题", confidence=0.2)
        hits = FactRetriever(self.store).retrieve("", top_k=5)
        self.assertTrue(hits)
        self.assertGreaterEqual(hits[0].confidence, hits[-1].confidence)

    def test_superseded_fact_loses_weight(self) -> None:
        """被对账降级的旧事实，排序权重要掉下来。"""
        self.store.add_fact("preference", "用户喜欢用 Cursor", confidence=0.85)
        self.store.add_fact(
            "preference", "用户改用：VS Code", confidence=0.85,
            supersede_keyword="Cursor",
        )
        hits = FactRetriever(self.store).retrieve("编辑器", top_k=5)
        contents = {item.fact.content: item.score for item in hits}
        old = next((s for c, s in contents.items() if "Cursor" in c), 0.0)
        new = next((s for c, s in contents.items() if "VS Code" in c), 0.0)
        self.assertLess(old, new, "旧偏好降级后排序权重没有掉下来")

    def test_expired_fact_decays(self) -> None:
        """时间久了的事实权重会衰减（类型条件衰减生效）。"""
        self.store.add_fact("entity", "用户提过一句闲聊内容", confidence=0.8)
        fresh = FactRetriever(self.store).retrieve("闲聊", top_k=1)[0]
        far_future = datetime.now() + timedelta(days=90)
        aged = FactRetriever(self.store, now=far_future).retrieve("闲聊", top_k=1)[0]
        self.assertLess(aged.score, fresh.score)

    def test_empty_store_returns_empty_list(self) -> None:
        self.assertEqual(FactRetriever(self.store).retrieve("随便问点什么"), [])


if __name__ == "__main__":
    unittest.main()
