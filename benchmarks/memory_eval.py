"""记忆评测基准（LongMemEval 思路缩小版）：场景化打分报告。

场景覆盖：事实抽取 / 偏好更新对账 / 检索相关性 / 跨会话持久化 / 防幻觉门禁 /
前瞻记忆 / 类型条件衰减。纯离线，不调外部模型。
用法：python benchmarks/memory_eval.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.activity.store import ActivityEvent
from screen_agent.memory.assembler import ContextAssembler
from screen_agent.memory.consolidate import Consolidator
from screen_agent.memory.retriever import HybridRetriever
from screen_agent.memory.store import MemoryStoreV2, fact_score


class MemoryEval:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name}" + (f"（{detail}）" if detail else ""))

    def run(self) -> float:
        tmp = tempfile.TemporaryDirectory()
        store = MemoryStoreV2(Path(tmp.name) / "events.db")
        cons = Consolidator(store)
        assembler = ContextAssembler(store, HybridRetriever(store))

        print("== S1 事实抽取准确率 ==")
        cons.extract_from_turn("我叫阿动", evidence="s1")
        cons.extract_from_turn("我喜欢用深色主题", evidence="s1")
        facts = {f.content: f for f in store.list_facts()}
        self.check("名字入 semantic 层", any("阿动" in c for c in facts))
        self.check("偏好入 semantic 层", any("深色主题" in c for c in facts))

        print("== S2 偏好更新对账 ==")
        cons.extract_from_turn("我不再喜欢深色主题了", evidence="s2")
        prefs = [(f.content, f.confidence) for f in store.list_facts(category="preference")]
        old_downgraded = all(c <= 0.15 for content, c in prefs if "深色" in content and "不再" not in content)
        self.check("旧偏好降级（supersede）", old_downgraded, str([(c[:20], round(cf, 2)) for c, cf in prefs]))

        print("== S3 检索相关性 ==")
        now = datetime.now()
        for i, summary in enumerate(["调试支付网关接口", "浏览旅游攻略", "整理周会纪要"]):
            e = ActivityEvent(
                event_id=f"act-{i:05d}",
                started_at=now - timedelta(hours=i + 1),
                ended_at=now - timedelta(hours=i + 1) + timedelta(minutes=8),
                page_category="web",
                user_action="debugging" if i == 0 else "browsing",
                summary=summary,
                task_tag=f"web:a{i}",
            )
            store.save_event(e)
        got = HybridRetriever(store).retrieve(query="支付 网关", top_k=3)
        self.check("相关 episode 排第一", got and got[0].event.summary == "调试支付网关接口")

        print("== S4 跨会话持久化 ==")
        sid = store.open_session()
        store.append_turn(sid, "user", "我叫阿动")
        store2 = MemoryStoreV2(store.db_path)  # 模拟重启
        turns = store2.load_session_turns(sid)
        self.check("会话轮次可恢复", len(turns) == 1 and "阿动" in turns[0]["content"])
        prompt = ContextAssembler(store2).build_system_prompt("你是助手", "我是谁")
        self.check("重启后 facts 仍在上下文", "阿动" in prompt)

        print("== S5 防幻觉门禁 ==")
        n_before = len(store.list_facts(limit=100))
        cons.extract_from_turn("你喜欢什么主题？")  # 疑问句
        n_after = len(store.list_facts(limit=100))
        self.check("疑问句不产生事实", n_before == n_after)
        try:
            store.add_fact("hobby", "非法类别")
            self.check("非法类别被拒", False)
        except ValueError:
            self.check("非法类别被拒", True)

        print("== S6 前瞻记忆 ==")
        due = now - timedelta(minutes=5)
        cons_intentions = cons.extract_intentions("提醒我明天交周报")
        self.check("意图提取（明天交周报）", bool(cons_intentions and cons_intentions[0][0] == "交周报"))
        store.add_intention("交周报", due)
        due_list = store.due_intentions()
        self.check("到点意图被触发", len(due_list) == 1 and due_list[0]["content"] == "交周报")

        print("== S7 类型条件衰减 ==")
        from screen_agent.memory.store import Fact

        old = now - timedelta(days=6)
        profile = Fact("f1", "profile", "名字", "src", 1.0, old, old)
        entity = Fact("f2", "entity", "临时", "src", 1.0, old, old)
        self.check("profile 衰减慢于 entity", fact_score(profile, now) > fact_score(entity, now))

        tmp.cleanup()
        passed = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        score = passed / total * 100 if total else 0.0
        print(f"\n== 记忆评测得分：{passed}/{total} = {score:.0f} ==")
        return score


if __name__ == "__main__":
    MemoryEval().run()
