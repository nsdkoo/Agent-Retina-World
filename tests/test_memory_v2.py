"""记忆系统 v2 测试：分层存储 / 三维检索 / 装配器 / 固化器。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.activity.store import ActivityEvent
from screen_agent.memory.assembler import ContextAssembler
from screen_agent.memory.consolidate import Consolidator
from screen_agent.memory.retriever import HybridRetriever
from screen_agent.memory.store import (
    FACT_HALF_LIFE_DAYS,
    MemoryStoreV2,
    fact_decay,
    fact_score,
    score_event_importance,
)


def _event(seq: int, hours_ago: float = 0.0, action: str = "browsing", summary: str = "浏览网页") -> ActivityEvent:
    now = datetime.now()
    t = now - timedelta(hours=hours_ago)
    return ActivityEvent(
        event_id=f"act-{seq:05d}",
        started_at=t,
        ended_at=t + timedelta(minutes=5),
        page_category="coding" if action == "debugging" else "web",
        user_action=action,
        summary=summary,
        task_tag=f"web:{action}",
    )


class StoreV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStoreV2(Path(self.tmp.name) / "events.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_event_importance_heuristic(self) -> None:
        self.assertEqual(score_event_importance(_event(1, action="browsing")), 1)
        focused = _event(2, action="debugging")
        focused.ended_at = focused.started_at + timedelta(minutes=15)
        self.assertGreaterEqual(score_event_importance(focused), 2)

    def test_fact_reconcile_updates_not_appends(self) -> None:
        self.store.add_fact("preference", "用户喜欢/常用：咖啡", confidence=0.7, evidence="s-1")
        again = self.store.add_fact("preference", "用户喜欢/常用：咖啡", confidence=0.75, evidence="s-2")
        facts = self.store.list_facts(category="preference")
        self.assertEqual(len(facts), 1)          # 对账：不追加
        self.assertGreaterEqual(again.confidence, 0.8)  # confidence 提升
        self.assertEqual(facts[0].evidence, "s-2")      # evidence 更新

    def test_fact_reject_unknown_category(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_fact("hobby", "x")

    def test_session_persistence_roundtrip(self) -> None:
        sid = self.store.open_session()
        self.store.append_turn(sid, "user", "我叫阿动")
        self.store.append_turn(sid, "assistant", "好的阿动")
        # 模拟重启：新实例从同库恢复
        store2 = MemoryStoreV2(self.store.db_path)
        self.assertEqual(store2.latest_open_session(), sid)
        turns = store2.load_session_turns(sid)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["content"], "我叫阿动")
        store2.close_session(sid)
        store3 = MemoryStoreV2(self.store.db_path)
        self.assertIsNone(store3.latest_open_session())

    def test_type_conditioned_decay(self) -> None:
        # profile 半衰期 30 天、entity 3 天：同样 6 天前，entity 衰减远比 profile 狠
        from screen_agent.memory.store import Fact

        now = datetime.now()
        old = now - timedelta(days=6)
        profile = Fact("f1", "profile", "名字", "chat_rule", 1.0, old, old)
        entity = Fact("f2", "entity", "临时", "chat_rule", 1.0, old, old)
        self.assertGreater(fact_decay(profile, now), fact_decay(entity, now))
        self.assertGreater(fact_score(profile, now), fact_score(entity, now))
        self.assertEqual(FACT_HALF_LIFE_DAYS["profile"], 30.0)


class RetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStoreV2(Path(self.tmp.name) / "events.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        self.store.save_event(_event(1, hours_ago=1, summary="调试支付网关接口"))
        self.store.save_event(_event(2, hours_ago=40, summary="浏览技术博客"))
        self.store.save_event(_event(3, hours_ago=3, action="debugging", summary="调试登录模块"))

    def test_recency_beats_old_when_no_query(self) -> None:
        # 同重要性的场景下，1 小时的比 40 小时的排前面
        self.store.save_event(_event(1, hours_ago=1, summary="浏览网页A"))
        self.store.save_event(_event(2, hours_ago=40, summary="浏览网页B"))
        got = HybridRetriever(self.store).retrieve(top_k=2)
        self.assertEqual(got[0].event.event_id, "act-00001")

    def test_relevance_boosts_matching(self) -> None:
        self._seed()
        got = HybridRetriever(self.store).retrieve(query="支付 网关", top_k=3)
        ids = [g.event.event_id for g in got]
        self.assertIn("act-00001", ids[:2])  # 相关的事件被顶上来

    def test_importance_in_scoring(self) -> None:
        e_old_focused = _event(4, hours_ago=30, action="debugging")
        e_old_focused.ended_at = e_old_focused.started_at + timedelta(minutes=20)
        self.store.save_event(e_old_focused)
        self.store.save_event(_event(5, hours_ago=30, action="browsing"))
        got = HybridRetriever(self.store).retrieve(top_k=2)
        ids = [g.event.event_id for g in got]
        self.assertIn("act-00004", ids)  # 高重要性老事件仍被检索到


class AssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStoreV2(Path(self.tmp.name) / "events.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prompt_contains_facts_and_episodes(self) -> None:
        self.store.add_fact("profile", "用户名字是阿动", confidence=0.9)
        self.store.save_event(_event(1, hours_ago=1))
        prompt = ContextAssembler(self.store).build_system_prompt("你是助手", "在做什么")
        self.assertIn("用户名字是阿动", prompt)
        self.assertIn("相关屏幕活动", prompt)
        self.assertTrue(prompt.startswith("你是助手"))

    def test_budget_truncates_sections(self) -> None:
        for i in range(10):
            self.store.add_fact("entity", f"临时事实编号{i}号内容比较长用来占位" * 2, confidence=0.9)
        prompt = ContextAssembler(self.store, budget_tokens=200).build_system_prompt("你是助手" * 100, None)
        self.assertLessEqual(len(prompt), 1200)  # 预算内


class ConsolidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStoreV2(Path(self.tmp.name) / "events.db")
        self.consolidator = Consolidator(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_extract_name_pattern(self) -> None:
        n = self.consolidator.extract_from_turn("我叫阿动，记住我", evidence="s-9")
        self.assertGreaterEqual(n, 1)
        facts = self.store.list_facts(category="profile")
        self.assertTrue(any("阿动" in f.content for f in facts))
        self.assertEqual(facts[0].evidence, "s-9")

    def test_extract_preference_and_project(self) -> None:
        n = self.consolidator.extract_from_turn("我喜欢用 Cursor 写代码")
        self.assertEqual(n, 1)
        n = self.consolidator.extract_from_turn("我在做一个简历诊断系统")
        self.assertEqual(n, 1)
        self.assertEqual(len(self.store.list_facts(category="preference")), 1)
        self.assertEqual(len(self.store.list_facts(category="project")), 1)

    def test_llm_candidates_validated(self) -> None:
        candidates = ["profile|用户是深圳程序员", "bad_line", "unknown_cat|x"]
        n = self.consolidator.extract_from_turn("随便聊聊", llm_fn=lambda t: candidates)
        self.assertEqual(n, 1)  # 只有合法候选入库
        self.assertEqual(len(self.store.list_facts()), 1)

    def test_mine_projects_threshold(self) -> None:
        # 两个 20 分钟同 task_tag 事件 → 累计 40 分钟 ≥ 30 → 挖出一条 project fact
        a = _event(1, action="debugging", summary="调试支付模块")
        b = _event(2, action="debugging", summary="调试支付模块")
        a.task_tag = b.task_tag = "coding:debugging"
        a.ended_at = a.started_at + timedelta(minutes=20)
        b.ended_at = b.started_at + timedelta(minutes=20)
        n = self.consolidator.mine_projects_from_events([a, b])
        self.assertEqual(n, 1)
        self.assertEqual(len(self.store.list_facts(category="project")), 1)


if __name__ == "__main__":
    unittest.main()
