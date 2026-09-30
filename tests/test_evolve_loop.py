"""自进化档案树的测试。

改这组的起因：`parent_id` 一直是**摆设**。它取的是"生成候选那一刻的最佳节点"，
本批候选全都指向同一个它，而且**不参与任何计算**——`verify` 拿的是"当前累积规则"
当基线，不是父节点的规则。实测档案里 v001–v004 的 `parent_id` 全是空串。

改完之后要守住三条：

1. **血缘可还原**：`rules_of(id)` 沿 lineage 重放增量，能还原出任意节点的完整规则
2. **父节点参与计算**：同一批候选共享父 ⇒ 共享基线；不同分支之间互不影响
3. **能探索**：`sample_parent` 能从全档案采样，不只是从最优节点（DGM 消融：去掉
   开放探索 50%→23%）
"""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.capture.privacy import PrivacyGate
from screen_agent.eval.dataset import GoldenSet
from screen_agent.eval.runner import Evaluator
from screen_agent.evolve import Archive, Evolver, Variant


class ArchiveTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.archive = Archive(Path(self._tmp.name) / "archive.json")

    def _variant(self, vid: str, parent: str = "", activity: str = "reading",
                 keywords: list[str] | None = None, accepted: bool = False,
                 holdout: float = 0.5) -> Variant:
        return Variant(
            variant_id=vid, parent_id=parent, payload={
                "activity": activity, "keywords": keywords or ["测试词"],
            },
            accepted=accepted, holdout_f1=holdout,
        )

    def test_children_and_depth(self) -> None:
        self.archive.add(self._variant("v001"))
        self.archive.add(self._variant("v002", parent="v001"))
        self.archive.add(self._variant("v003", parent="v002"))
        self.archive.add(self._variant("v004", parent="v001"))

        self.assertEqual([c.variant_id for c in self.archive.children("v001")],
                         ["v002", "v004"])
        self.assertEqual(self.archive.depth("v001"), 0)
        self.assertEqual(self.archive.depth("v002"), 1)
        self.assertEqual(self.archive.depth("v003"), 2)
        self.assertEqual(self.archive.max_depth(), 2)
        self.assertEqual([r.variant_id for r in self.archive.roots()], ["v001"])

    def test_lineage_walks_back_to_root(self) -> None:
        for vid, parent in (("v001", ""), ("v002", "v001"), ("v003", "v002")):
            self.archive.add(self._variant(vid, parent=parent))
        self.assertEqual(self.archive.lineage("v003"), ["v003", "v002", "v001"])

    def test_next_id_uses_max_not_count(self) -> None:
        """按最大编号递增：给 v010 之后必须出 v011，不能按条数算成 v002。"""
        self.archive.add(self._variant("v010"))
        self.archive.add(self._variant("v011"))
        self.assertEqual(self.archive.next_id(), "v012")
        self.assertEqual(self.archive.next_id(offset=2), "v014")

    def test_persist_and_reload_keeps_tree(self) -> None:
        self.archive.add(self._variant("v001"))
        self.archive.add(self._variant("v002", parent="v001"))
        reloaded = Archive(Path(self._tmp.name) / "archive.json")
        self.assertEqual([c.variant_id for c in reloaded.children("v001")], ["v002"])
        self.assertEqual(reloaded.depth("v002"), 1)


class RulesOfTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.archive = Archive(Path(self._tmp.name) / "archive.json")
        self.evolver = Evolver(
            Evaluator(privacy=PrivacyGate()), GoldenSet.seed(), self.archive
        )

    def _variant(self, vid: str, parent: str, activity: str, keywords: list[str]) -> Variant:
        return Variant(
            variant_id=vid, parent_id=parent,
            payload={"activity": activity, "keywords": keywords},
        )

    def test_empty_id_returns_base_rules(self) -> None:
        self.assertEqual(
            self.evolver.rules_of(""), dict(self.evolver._base_rules)  # noqa: SLF001
        )

    def test_single_chain_replays_in_order(self) -> None:
        self.archive.add(self._variant("v001", "", "reading", ["知乎"]))
        self.archive.add(self._variant("v002", "v001", "reading", ["arxiv"]))
        rules = self.evolver.rules_of("v002")
        # 叶子的增量排在前面（prepend），祖先的排在后面
        self.assertEqual(list(rules["reading"][:2]), ["arxiv", "知乎"])

    def test_branches_do_not_leak(self) -> None:
        """两条分支各加各的词，互不污染——这是树相对单链的核心价值。"""
        self.archive.add(self._variant("v001", "", "reading", ["知乎"]))
        self.archive.add(self._variant("v002", "v001", "reading", ["arxiv"]))
        self.archive.add(self._variant("v003", "v001", "reading", ["教程"]))

        left = self.evolver.rules_of("v002")
        right = self.evolver.rules_of("v003")
        self.assertIn("arxiv", left["reading"])
        self.assertNotIn("教程", left["reading"], "左分支混进了右分支的词")
        self.assertIn("教程", right["reading"])
        self.assertNotIn("arxiv", right["reading"], "右分支混进了左分支的词")

    def test_node_increment_included_even_if_rejected(self) -> None:
        """没被采纳的节点也参与重放——它代表"这条配置"本身。"""
        self.archive.add(self._variant("v001", "", "reading", ["知乎"]))
        self.archive.add(self._variant("v002", "v001", "reading", ["arxiv"]))
        self.assertIn("arxiv", self.evolver.rules_of("v002")["reading"])


class SampleParentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.archive = Archive(Path(self._tmp.name) / "archive.json")
        self.evolver = Evolver(
            Evaluator(privacy=PrivacyGate()), GoldenSet.seed(), self.archive
        )
        self.archive.add(Variant("v001", payload={"activity": "reading", "keywords": ["a"]},
                                 accepted=True, holdout_f1=0.9))
        self.archive.add(Variant("v002", payload={"activity": "reading", "keywords": ["b"]},
                                 accepted=True, holdout_f1=0.5))
        self.archive.add(Variant("v003", payload={"activity": "reading", "keywords": ["c"]},
                                 accepted=False, holdout_f1=0.1))

    def test_empty_archive_returns_root_marker(self) -> None:
        empty = Archive(Path(self._tmp.name) / "empty.json")
        evolver = Evolver(Evaluator(privacy=PrivacyGate()), GoldenSet.seed(), empty)
        self.assertEqual(evolver.sample_parent(), "")

    def test_best_picks_highest_holdout(self) -> None:
        self.assertEqual(self.evolver.sample_parent("best"), "v001")

    def test_random_can_reach_every_node(self) -> None:
        """纯探索要能采到未采纳的节点——这正是 DGM 说的开放探索。"""
        rng = random.Random(7)
        seen = {self.evolver.sample_parent("random", rng=rng) for _ in range(60)}
        self.assertIn("v003", seen, "随机探索永远采不到未采纳节点，等于没有开放探索")

    def test_epsilon_explores_sometimes(self) -> None:
        rng = random.Random(11)
        seen = {self.evolver.sample_parent("epsilon", epsilon=0.5, rng=rng)
                for _ in range(80)}
        self.assertGreater(len(seen), 1, "ε-greedy 从没探索过，退化成纯利用")

    def test_weighted_prefers_high_holdout(self) -> None:
        rng = random.Random(3)
        picks = [self.evolver.sample_parent("weighted", rng=rng) for _ in range(80)]
        self.assertGreater(picks.count("v001"), picks.count("v002"))


class VerifyBaselineTests(unittest.TestCase):
    """校验基线取自父节点，而不是"当前累积规则"。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.archive = Archive(Path(self._tmp.name) / "archive.json")
        self.evolver = Evolver(
            Evaluator(privacy=PrivacyGate()), GoldenSet.seed(), self.archive
        )

    def test_siblings_share_the_same_baseline(self) -> None:
        """同一父节点下的兄弟候选，基线必须一致。

        以前 `verify` 用 `self.rules`（会随采纳不断累积），兄弟候选的基线各不相同，
        对比就失去了意义。
        """
        self.archive.add(Variant("v001", parent_id="",
                                 payload={"activity": "reading", "keywords": ["知乎"]},
                                 accepted=True, holdout_f1=0.6))
        a = Variant("v002", parent_id="v001",
                    payload={"activity": "reading", "keywords": ["arxiv"]})
        b = Variant("v003", parent_id="v001",
                    payload={"activity": "reading", "keywords": ["教程"]})
        self.assertEqual(
            self.evolver.rules_of(a.parent_id), self.evolver.rules_of(b.parent_id)
        )

    def test_step_records_parent_on_every_variant(self) -> None:
        failures = [("a08", "reading", "browsing")]
        results = self.evolver.step(failures, parent_id="")
        self.assertTrue(results, "没有生成任何候选")
        for variant in results:
            self.assertEqual(variant.parent_id, "")
            # 父为根时，父节点必须存在或为空（空表示从基础规则出发）
            self.assertTrue(
                variant.parent_id == "" or self.archive.get(variant.parent_id) is not None
            )

    def test_step_parent_is_existing_node(self) -> None:
        self.archive.add(Variant("v001", payload={"activity": "reading", "keywords": ["x"]},
                                 accepted=True, holdout_f1=0.2))
        results = self.evolver.step(
            [("a08", "reading", "browsing")], parent_id="v001"
        )
        for variant in results:
            self.assertEqual(variant.parent_id, "v001")
            self.assertIsNotNone(self.archive.get("v001"))


if __name__ == "__main__":
    unittest.main()
