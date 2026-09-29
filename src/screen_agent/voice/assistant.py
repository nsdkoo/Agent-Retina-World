from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Callable

from screen_agent.config import load_yaml
from screen_agent.pipeline import PerceptionPipeline
from screen_agent.understand.chat import SYSTEM_PROMPT, DisabledChatClient, build_chat_client
from screen_agent.memory.consolidate import EXTRACTION_PROMPT
from screen_agent.memory.assembler import ContextAssembler
from screen_agent.memory.consolidate import Consolidator
from screen_agent.memory.retriever import HybridRetriever
from screen_agent.memory.store import MemoryStoreV2
from screen_agent.voice.executor import ActionResult, CommandExecutor
from screen_agent.voice.intents import IntentType, parse_intent
from screen_agent.voice.listener import Speaker

logger = logging.getLogger(__name__)


class VoiceAssistant:
    """常驻语音助手：唤醒后可进入免唤醒连续对话，语音与打字共用 handle_command。"""

    def __init__(self, config_path: Path, project_root: Path | None = None) -> None:
        raw = load_yaml(config_path)
        voice_cfg = raw.get("voice", {})
        chat_cfg = raw.get("chat", {})
        web_cfg = raw.get("web", {})
        sherpa_cfg = voice_cfg.get("sherpa", {}) if isinstance(voice_cfg.get("sherpa", {}), dict) else {}
        root = project_root or config_path.parent

        self.wake_names: list[str] = voice_cfg.get("wake_names", ["Retina", "小光", "光光"])
        self.session_enabled = bool(voice_cfg.get("session_mode", True))
        self.session_duration = float(voice_cfg.get("session_duration_seconds", 60))
        self.chat_enabled = bool(chat_cfg.get("enabled", False))
        self.chat_model = self._chat_model_hint(chat_cfg)

        self.pipeline = PerceptionPipeline.from_config(config_path)
        web_host = web_cfg.get("host", "127.0.0.1")
        web_port = int(web_cfg.get("port", 8765))
        self.web_url = f"http://{web_host}:{web_port}"

        # ---- 语音引擎（sherpa 流式栈） ----
        models_root = root / sherpa_cfg.get("models_dir", "models")
        from screen_agent.voice.audio_loop import AudioLoop
        from screen_agent.voice.sherpa_engine import build_keywords_txt

        asr_dir = models_root / sherpa_cfg.get(
            "asr_model_dir", "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
        )
        kws_dir = models_root / sherpa_cfg.get(
            "kws_model_dir", "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        )
        tts_dir = models_root / sherpa_cfg.get("tts_model_dir", "vits-zh-hf-fanchen-C")
        keywords_file = root / "data" / "keywords.txt"
        keywords = list(dict.fromkeys(
            list(sherpa_cfg.get("keywords", ["小光", "光光"])) + [
                w for w in self.wake_names if not w.isascii()
            ]
        ))
        kws_threshold = float(sherpa_cfg.get("kws_threshold", 0.4))
        from screen_agent.voice.sherpa_engine import EN_PHONEMES

        extra_lines: list[str] = []
        for w in self.wake_names:
            if w.isascii():
                for phones in EN_PHONEMES.get(w.lower(), []):
                    extra_lines.append(f"{' '.join(phones)} @{w}")
        build_keywords_txt(
            keywords, kws_dir, keywords_file, kws_threshold,
            extra_keyword_lines=extra_lines,
        )

        self.audio_loop = AudioLoop(
            self,
            asr_model_dir=asr_dir,
            kws_model_dir=kws_dir,
            keywords_file=keywords_file,
            kws_threshold=kws_threshold,
            num_threads=int(sherpa_cfg.get("num_threads", 1)),
            mic_device=sherpa_cfg.get("mic_device"),
            idle_asr=bool(sherpa_cfg.get("idle_asr", False)),
            audio_file=(root / sherpa_cfg["audio_source"])
            if sherpa_cfg.get("audio_source")
            else None,
        )

        tts_engine = sherpa_cfg.get("tts_engine", "sherpa")
        self.speaker = Speaker(
            enabled=bool(voice_cfg.get("speak_feedback", True)),
            engine=tts_engine,
            tts_model_dir=tts_dir if tts_dir.is_dir() else None,
            on_play_start=self.audio_loop.set_muted_mic,
            on_play_end=self.audio_loop.set_unmuted_mic,
        )

        self._chat_history: list[dict[str, str]] = []

        # ---- 记忆系统 v2：分层存储 + 三维检索 + 装配器 + 固化器 ----
        memory_cfg = raw.get("memory", {}) if isinstance(raw.get("memory", {}), dict) else {}
        self.memory = MemoryStoreV2(root / memory_cfg.get("db_path", "data/memory/events.db"))
        # 向量通道（可选）：硅基流动 bge-m3 免费/OpenAI 兼容端点；未配 key 自动降级跳过
        self._vectors = None
        embed_cfg = raw.get("embedding", {}) if isinstance(raw.get("embedding", {}), dict) else {}
        if bool(embed_cfg.get("enabled", False)):
            api_key = str(embed_cfg.get("api_key") or "") or os.environ.get(
                str(embed_cfg.get("api_key_env") or ""), ""
            )
            if api_key:
                from screen_agent.dedup.semantic import EmbeddingClient
                from screen_agent.memory.vector import MemoryVectors

                embedder = EmbeddingClient(
                    base_url=str(embed_cfg.get("base_url")),
                    model=str(embed_cfg.get("model", "BAAI/bge-m3")),
                    api_key=api_key,
                )
                self._vectors = MemoryVectors(self.memory.db_path, embedder)
        self.retriever = HybridRetriever(
            self.memory,
            vector_search=(self._vectors.search if self._vectors is not None else None),
        )
        self.assembler = ContextAssembler(self.memory, self.retriever)
        self.consolidator = Consolidator(self.memory)
        # 工作记忆恢复：接上次未关闭的会话
        self._session_id = self.memory.latest_open_session() or self.memory.open_session()
        for turn in self.memory.load_session_turns(self._session_id, limit=12):
            if turn["role"] in ("user", "assistant"):
                self._chat_history.append(turn)
        self._unconsolidated: list[tuple[str, str]] = []  # 待固化 (user, assistant)
        self._last_activity = time.monotonic()
        self._consolidation_stop = threading.Event()

        chat_client = build_chat_client(chat_cfg)
        self.executor = CommandExecutor(
            self.pipeline,
            web_url=self.web_url,
            chat_client=chat_client,
            chat_history=self._chat_history,
            max_history=int(chat_cfg.get("max_history", 6)),
            screen_context_fn=self._recent_screen_context,
            memory_context_fn=self._memory_context,
            on_chat_delta=self._emit_result_delta,
        )
        self.app_aliases: dict[str, str] = voice_cfg.get("apps", {})
        self.url_aliases: dict[str, str] = voice_cfg.get("urls", {})

        self._in_session = False
        self._session_until = 0.0
        self._status = "idle"
        self._running = False
        self._on_status: Callable[[str], None] | None = None
        self._on_transcript: Callable[[str], None] | None = None
        self._on_result: Callable[[str], None] | None = None
        self._on_result_delta: Callable[[str], None] | None = None
        self._on_session: Callable[[bool], None] | None = None

    @staticmethod
    def _chat_model_hint(chat_cfg: dict) -> str:
        backends = chat_cfg.get("backends")
        if isinstance(backends, list) and backends:
            return " + ".join(
                str(b.get("model", "?")) for b in backends if isinstance(b, dict)
            )
        return str(chat_cfg.get("model", "gpt-5.4-mini"))

    def _memory_context(self, user_text: str) -> str:
        """记忆装配器：语义 facts（类型衰减排序）+ 三维检索 episodes → system prompt。"""
        try:
            return self.assembler.build_system_prompt(SYSTEM_PROMPT, user_text)
        except Exception:
            logger.debug("记忆装配失败", exc_info=True)
            return SYSTEM_PROMPT

    def _recent_screen_context(self) -> str:
        try:
            md = self.pipeline.proactive.timeline_markdown(limit=1)
            lines = [ln.lstrip("- ").strip() for ln in md.split("\n") if ln.startswith("- ")]
            return lines[0][:120] if lines else ""
        except Exception:
            return ""

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._on_status = cb

    def on_transcript(self, cb: Callable[[str], None]) -> None:
        self._on_transcript = cb

    def on_result(self, cb: Callable[[str], None]) -> None:
        self._on_result = cb

    def on_result_delta(self, cb: Callable[[str], None]) -> None:
        """注册流式增量回调（累计全文）。UI 用来边生成边显示。"""
        self._on_result_delta = cb

    def _emit_result_delta(self, accumulated: str) -> None:
        """节流转发：~90ms 一次，避免高频信号打爆 UI 事件循环；最终全文由 emit_result 兜底。"""
        now = time.monotonic()
        if now - getattr(self, "_last_delta_emit", 0.0) < 0.09:
            return
        self._last_delta_emit = now
        if self._on_result_delta is None:
            return
        try:
            self._on_result_delta(accumulated)
        except Exception:
            logger.debug("delta 回调异常", exc_info=True)

    def on_session(self, cb: Callable[[bool], None]) -> None:
        self._on_session = cb

    @property
    def in_session(self) -> bool:
        return self._in_session and time.time() < self._session_until

    @property
    def session_expired(self) -> bool:
        """会话开着但已超时（由音频循环检测并收尾）。"""
        return self._in_session and not self.in_session

    # ---- 供 AudioLoop / UI 调用的公共接口 ----

    def set_status(self, status: str) -> None:
        self._status = status
        if status != "idle":
            self._last_activity = time.monotonic()
        if self._on_status:
            self._on_status(status)

    def emit_transcript(self, text: str) -> None:
        if self._on_transcript:
            self._on_transcript(text)

    def emit_result(self, text: str) -> None:
        if self._on_result:
            self._on_result(text)

    def speak(self, text: str) -> None:
        self.speaker.say(text)

    def contains_wake_word(self, text: str) -> bool:
        lower = text.lower()
        for name in self.wake_names:
            if name.lower() in lower or name in text:
                return True
        return False

    def strip_wake_word(self, text: str) -> str:
        result = text
        for name in sorted(self.wake_names, key=len, reverse=True):
            result = re.sub(re.escape(name), "", result, flags=re.I)
        result = re.sub(r"^[，,、\s]+", "", result)
        result = re.sub(r"[，,、\s]+$", "", result)
        return result.strip()

    def begin_wake_session(self) -> str:
        """KWS 命中且后面没有跟命令时调用。返回播报文案。"""
        if self.session_enabled:
            self._set_session(True)
            return "我在，请说你要做什么"
        self.set_status("listening")
        return "我在"

    def handle_command(self, command: str) -> ActionResult | None:
        """执行一条已剥离唤醒词的指令（语音与打字共用入口）。"""
        command = (command or "").strip()
        if not command:
            return None
        intent = parse_intent(command, self.app_aliases, self.url_aliases)
        result = self.executor.run(intent)
        self._last_activity = time.monotonic()
        if (
            intent.type == IntentType.CHAT
            and result is not None
            and result.success
            and result.message
        ):
            try:
                self.consolidator.extract_from_turn(
                    command, result.message,
                    evidence=f"session:{self._session_id}",
                )
                # 前瞻记忆：「提醒我明天X」→ intentions 表
                for content, due_at in self.consolidator.extract_intentions(command):
                    self.memory.add_intention(content, due_at)
                self.memory.append_turn(self._session_id, "user", command)
                self.memory.append_turn(self._session_id, "assistant", result.message)
                self._unconsolidated.append((command, result.message))
                # MemOS 式反思回填：被对话实际引用的 episode importance 提权
                for eid in getattr(self.assembler, "last_used_event_ids", []):
                    try:
                        self.memory.bump_event_importance(eid)
                    except Exception:
                        pass
            except Exception:
                logger.debug("记忆固化/持久化失败", exc_info=True)
        if intent.type == IntentType.END_SESSION:
            self._set_session(False)
            # 用户明确退出：关闭当前会话（归档），下一条指令开新会话
            try:
                self.memory.close_session(self._session_id)
                self._session_id = self.memory.open_session()
                self._chat_history.clear()
                self._unconsolidated.clear()
            except Exception:
                logger.debug("会话轮转失败", exc_info=True)
        elif self.session_enabled and not self._in_session:
            self._set_session(True)
        else:
            self._extend_session()
        return result

    def end_session(self) -> None:
        self._set_session(False)

    # ---- 兼容保留：直接处理一条完整转写（打字面板可走这里） ----

    def _set_status(self, status: str) -> None:
        self.set_status(status)

    def _emit_transcript(self, text: str) -> None:
        self.emit_transcript(text)

    def _emit_result(self, text: str) -> None:
        self.emit_result(text)

    def _contains_wake_word(self, text: str) -> bool:
        return self.contains_wake_word(text)

    def _strip_wake_word(self, text: str) -> str:
        return self.strip_wake_word(text)

    def handle_transcript(self, text: str) -> ActionResult | None:
        """处理一条语音转写。会话中免唤醒。"""
        if self.in_session:
            command = text.strip()
            if not command:
                return None
            intent = parse_intent(command, self.app_aliases, self.url_aliases)
            result = self.executor.run(intent)
            if intent.type == IntentType.END_SESSION:
                self._set_session(False)
            else:
                self._extend_session()
            return result

        if not self.contains_wake_word(text):
            return None

        command = self.strip_wake_word(text)
        if not command:
            if self.session_enabled:
                self._set_session(True)
            return ActionResult(success=True, message="我在，请说你要做什么")

        intent = parse_intent(command, self.app_aliases, self.url_aliases)
        result = self.executor.run(intent)
        if self.session_enabled and intent.type != IntentType.END_SESSION:
            self._set_session(True)
        return result

    def _set_session(self, active: bool) -> None:
        self._in_session = active
        if active:
            self._session_until = time.time() + self.session_duration
            self.set_status("session")
        else:
            self._session_until = 0.0
            self._chat_history.clear()
            self.set_status("idle")
        if self._on_session:
            self._on_session(active)

    def _extend_session(self) -> None:
        if self._in_session:
            self._session_until = time.time() + self.session_duration

    def run_forever(self) -> None:
        self._running = True
        try:
            self.audio_loop.run()
        except RuntimeError as exc:
            # 麦克风缺失/禁用等启动失败只记日志，不弹到对话流（用户打字路径不受影响）
            logger.error("语音主循环退出: %s", exc)
        finally:
            self.speaker.stop()

    def stop(self) -> None:
        """进程退出。会话保持 open——跨重启延续（用户说「退出」才真正关闭会话）。"""
        self._running = False
        self._consolidation_stop.set()
        self.audio_loop.stop()

    def run_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, daemon=True)
        thread.start()
        consolidation = threading.Thread(target=self._consolidation_loop, daemon=True)
        consolidation.start()
        return thread

    def _consolidation_loop(self) -> None:
        """睡眠门控固化（Google/Cornell Sleep 范式 + Sleep-Gated 披露）：
        只在 idle 且超过 5 分钟无交互时运行，固化永不与交互抢资源。"""
        while not self._consolidation_stop.wait(60.0):
            try:
                idle_long = time.monotonic() - self._last_activity > 300.0
                if self._status != "idle" or not idle_long:
                    continue
                events = self.memory.list_events(limit=200)
                self.consolidator.mine_projects_from_events(events)
                if self._vectors is not None:
                    self._vectors.backfill(events, max_new=20)
                # 前瞻记忆到点主动提醒（proactive trigger）
                for it in self.memory.due_intentions():
                    self.emit_result(f"⏰ 到点了：{it['content']}")
                    self.memory.complete_intention(it["intention_id"])
                # 梦境重组：跨域配对找连接（五成概率限流，避免每轮都打扰）
                chat_client = getattr(self.executor, "chat_client", None)
                if (
                    self.chat_enabled
                    and chat_client is not None
                    and not isinstance(chat_client, DisabledChatClient)
                    and random.random() < 0.5
                ):
                    insight = self.consolidator.dream_recombine(
                        lambda prompt: chat_client.complete(
                            [{"role": "user", "content": prompt}]
                        )
                    )
                    if insight:
                        self.emit_result(f"💡 顺着记忆想到一件事：{insight}")
                for user_text, reply in self._unconsolidated:
                    self.consolidator.extract_from_turn(
                        user_text, reply, evidence=f"session:{self._session_id}"
                    )
                # 梦境期 LLM 候选提取：模型提议、规则裁决（格式校验后才入库）
                if self._unconsolidated and self.chat_enabled:
                    chat_client = getattr(self.executor, "chat_client", None)
                    if chat_client is not None and not isinstance(chat_client, DisabledChatClient):
                        sep = chr(10) * 2
                        turns_text = sep.join(
                            f"用户：{u}{chr(10)}助手：{r}" for u, r in self._unconsolidated
                        )
                        reply_text = chat_client.complete(
                            [{"role": "user", "content": EXTRACTION_PROMPT.format(turns=turns_text)}]
                        )
                        for line in (ln.strip() for ln in reply_text.splitlines()):
                            if "|" not in line:
                                continue
                            category, _, content = line.partition("|")
                            category, content = category.strip(), content.strip()
                            if category in ("profile", "preference", "project", "entity") and 0 < len(content) <= 80:
                                self.memory.add_fact(
                                    category, content, source="chat_llm",
                                    confidence=0.55, evidence=f"dream:{self._session_id}",
                                )
                self._unconsolidated.clear()
            except Exception:
                logger.debug("睡眠固化失败", exc_info=True)
