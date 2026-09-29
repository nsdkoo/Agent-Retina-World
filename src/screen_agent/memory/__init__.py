"""记忆系统 v2：分层存储 + 三维检索 + 上下文装配 + 规则固化。

对标 Letta（分层）/ Mem0（写入对账）/ Zep（时间意识）/ Generative Agents（三维打分+反思），
取 Offline 可实现的最小完备集。设计见 docs/memory-design.md。
"""

from screen_agent.memory.assembler import ContextAssembler
from screen_agent.memory.consolidate import Consolidator
from screen_agent.memory.retriever import HybridRetriever
from screen_agent.memory.store import Fact, MemoryStoreV2

__all__ = ["ContextAssembler", "Consolidator", "HybridRetriever", "Fact", "MemoryStoreV2"]
