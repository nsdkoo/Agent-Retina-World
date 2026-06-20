import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.dedup.semantic import SemanticDeduper, cosine_similarity


class FakeEmbeddingClient:
  def __init__(self, mapping: dict[str, list[float]]) -> None:
    self.mapping = mapping
    self.calls = 0

  def embed(self, text: str) -> list[float]:
    self.calls += 1
    return self.mapping.get(text, [0.1, 0.2, 0.3])


class SemanticTests(unittest.TestCase):
  def test_cosine_identical(self) -> None:
    self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)

  def test_semantic_dedup_hits_similar(self) -> None:
    client = FakeEmbeddingClient({
      "hello world": [1.0, 0.0],
      "hello world!": [0.99, 0.01],
    })
    deduper = SemanticDeduper(client=client, threshold=0.95)
    self.assertFalse(deduper.check("hello world").is_duplicate)
    result = deduper.check("hello world!")
    self.assertTrue(result.is_duplicate)
    self.assertGreater(result.similarity or 0, 0.95)


if __name__ == "__main__":
  unittest.main()
