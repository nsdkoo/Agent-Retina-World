from __future__ import annotations

import math
from dataclasses import dataclass, field

import httpx


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class EmbeddingClient:
    """OpenAI 兼容 Embedding API 客户端。"""

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def embed(self, text: str) -> list[float]:
        if not text.strip():
            return []
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model, "input": text}
        resp = self._client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


@dataclass
class SemanticDedupResult:
    is_duplicate: bool
    similarity: float | None = None


@dataclass
class SemanticDedupStats:
    total_checks: int = 0
    hits: int = 0

    def to_dict(self) -> dict:
        return {
            "total_checks": self.total_checks,
            "hits": self.hits,
            "hit_rate": round(self.hits / self.total_checks, 4) if self.total_checks else 0.0,
        }


class SemanticDeduper:
    """L3 语义去重：基于文本指纹的 embedding 余弦相似度。"""

    def __init__(
        self,
        client: EmbeddingClient,
        threshold: float = 0.92,
        max_history: int = 100,
    ) -> None:
        self.client = client
        self.threshold = threshold
        self.max_history = max_history
        self._vectors: list[list[float]] = []
        self.stats = SemanticDedupStats()

    def check(self, fingerprint: str) -> SemanticDedupResult:
        self.stats.total_checks += 1
        vec = self.client.embed(fingerprint)
        if not vec:
            return SemanticDedupResult(is_duplicate=False)

        for prev in self._vectors:
            sim = cosine_similarity(vec, prev)
            if sim >= self.threshold:
                self.stats.hits += 1
                return SemanticDedupResult(is_duplicate=True, similarity=sim)

        self._vectors.append(vec)
        if len(self._vectors) > self.max_history:
            self._vectors.pop(0)
        return SemanticDedupResult(is_duplicate=False)

    def remember(self, text: str) -> None:
        """分析完成后登记摘要向量，供后续语义比对。"""
        vec = self.client.embed(text)
        if vec:
            self._vectors.append(vec)
            if len(self._vectors) > self.max_history:
                self._vectors.pop(0)
