"""MemoryVectors：情节记忆的向量索引（Sleep 范式：向量在空闲期计算）。

- backfill(events)：睡眠门控固化线程里补算缺失向量（空闲期算力，不打扰交互）
- search(query_text)：检索时只嵌入查询一次，余弦相似度排序
- 向量存 SQLite BLOB（float32），单机零依赖
"""

from __future__ import annotations

import sqlite3
import struct
from contextlib import contextmanager
from pathlib import Path

from screen_agent.activity.store import ActivityEvent
from screen_agent.dedup.semantic import cosine_similarity


class MemoryVectors:
    def __init__(self, db_path: Path, embedder) -> None:
        self.db_path = db_path
        self.embedder = embedder  # 需提供 embed(text) -> list[float]
        self._init_db()

    @staticmethod
    @contextmanager
    def _db(db_path: Path):
        conn = sqlite3.connect(db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_vectors (
                    event_id TEXT PRIMARY KEY,
                    dim INTEGER NOT NULL,
                    vector BLOB NOT NULL
                )
                """
            )

    @staticmethod
    def _pack(vec: list[float]) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    @staticmethod
    def _unpack(blob: bytes) -> list[float]:
        n = len(blob) // 4
        return list(struct.unpack(f"{n}f", blob))

    def has_vector(self, event_id: str) -> bool:
        with self._db(self.db_path) as conn:
            return conn.execute(
                "SELECT 1 FROM event_vectors WHERE event_id = ?", (event_id,)
            ).fetchone() is not None

    def upsert(self, event_id: str, vector: list[float]) -> None:
        with self._db(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO event_vectors (event_id, dim, vector)
                VALUES (?, ?, ?)
                """,
                (event_id, len(vector), self._pack(vector)),
            )

    def backfill(self, events: list[ActivityEvent], max_new: int = 20) -> int:
        """空闲期补算缺失向量；缺矢量的先收集，一次批量请求（input 数组）。返回本次补算数。"""
        pending: list[ActivityEvent] = []
        for e in events:
            if len(pending) >= max_new:
                break
            if not self.has_vector(e.event_id):
                pending.append(e)
        if not pending:
            return 0
        try:
            from screen_agent.dedup.semantic import embed_batch

            texts = [f"{e.summary} {e.page_category} {e.user_action}" for e in pending]
            vectors = embed_batch(self.embedder, texts)
        except Exception:
            # 批量失败退回逐条（兼容不支持数组批量的端点）
            done = 0
            for e in pending:
                try:
                    text = f"{e.summary} {e.page_category} {e.user_action}"
                    self.upsert(e.event_id, self.embedder.embed(text))
                    done += 1
                except Exception:
                    continue
            return done
        for e, vec in zip(pending, vectors):
            self.upsert(e.event_id, vec)
        return len(pending)

    def search(self, query_text: str, top_k: int = 10) -> list[tuple[str, float]]:
        """余弦相似度检索，返回 [(event_id, score 0-1)]。"""
        qvec = self.embedder.embed(query_text)
        with self._db(self.db_path) as conn:
            rows = conn.execute("SELECT event_id, vector FROM event_vectors").fetchall()
        hits: list[tuple[str, float]] = []
        for event_id, blob in rows:
            try:
                score = (cosine_similarity(qvec, self._unpack(blob)) + 1.0) / 2.0  # 归一 0-1
                hits.append((event_id, score))
            except Exception:
                continue
        hits.sort(key=lambda x: -x[1])
        return hits[:top_k]
