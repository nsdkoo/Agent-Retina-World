from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是 Agent-Retina 桌面语音助手，简洁友好，用中文回答。"
    "用户通过语音与你交流；若被问到屏幕内容，可提示用户说「分析屏幕」。"
    "回答尽量简短，控制在 2-4 句话。"
)


def resolve_chat_api_key(value: str | None, env_key: str | None = None) -> str:
    """解析 Chat API Key：config → 环境变量 → ~/.codex/auth.json。"""
    from screen_agent.config import resolve_secret

    key = resolve_secret(value, env_key)
    if key:
        return key

    for env_name in ("CHAT_API_KEY", "OPENAI_API_KEY"):
        key = os.environ.get(env_name, "").strip()
        if key:
            return key

    auth_path = Path.home() / ".codex" / "auth.json"
    if auth_path.exists():
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
            key = str(data.get("OPENAI_API_KEY", "")).strip()
            if key:
                return key
        except Exception as exc:
            logger.warning("读取 ~/.codex/auth.json 失败: %s", exc)

    return ""


class OpenAICompatibleChatClient:
    """OpenAI 兼容文本 Chat API 客户端（codexzh 等）。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        fallback_model: str = "gpt-5.4",
        max_tokens: int = 256,
        timeout: float = 60.0,
    ) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback_model = fallback_model
        self.max_tokens = max_tokens
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def complete(self, messages: list[dict[str, str]], system: str | None = None) -> str:
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(messages)

        last_error: Exception | None = None
        for model in (self.model, self.fallback_model):
            if not model:
                continue
            try:
                return self._request(model, payload_messages)
            except Exception as exc:
                last_error = exc
                logger.warning("Chat 模型 %s 失败，尝试回退: %s", model, exc)

        if last_error:
            raise last_error
        raise RuntimeError("Chat 请求失败")

    def _request(self, model: str, messages: list[dict[str, str]]) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["choices"][0]["message"]["content"]).strip()

    def complete_stream(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        """流式补全：SSE 增量回调 on_delta(累计全文)，失败回退非流式。"""
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(messages)

        last_error: Exception | None = None
        for model in (self.model, self.fallback_model):
            if not model:
                continue
            try:
                return self._request_stream(model, payload_messages, on_delta)
            except Exception as exc:
                last_error = exc
                logger.warning("Chat 流式 %s 失败，尝试回退: %s", model, exc)

        if last_error:
            logger.warning("流式全部失败，回退非流式: %s", last_error)
        return self.complete(messages, system=system)

    def _request_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        on_delta: Callable[[str], None] | None,
    ) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        parts: list[str] = []
        with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                choices = obj.get("choices") or [{}]
                delta = str((choices[0].get("delta") or {}).get("content") or "")
                if delta:
                    parts.append(delta)
                    if on_delta:
                        on_delta("".join(parts))
        text = "".join(parts).strip()
        if not text:
            raise RuntimeError("流式返回为空")
        return text


class DisabledChatClient:
    """Chat 未启用时的占位客户端。"""

    def complete(self, messages: list[dict[str, str]], system: str | None = None) -> str:
        raise RuntimeError("Chat 未启用，请在 config.yaml 配置 chat 段并设置 API Key")


class MultiBackendChatClient:
    """多后端 Chat 客户端：按配置顺序探测，前一个失败自动切下一个。

    典型配置：本地 vLLM（千问系列）优先，云端 DeepSeek 兜底。
    """

    def __init__(self, backends: list[OpenAICompatibleChatClient]) -> None:
        if not backends:
            raise ValueError("MultiBackendChatClient 需要至少一个后端")
        self.backends = backends
        self.preferred: str | None = None  # backend name，None=按配置顺序

    @property
    def names(self) -> list[str]:
        return [f"{b.base_url}:{b.model}" for b in self.backends]

    @property
    def backend_names(self) -> list[str]:
        return [getattr(b, "name", "") or b.model for b in self.backends]

    @property
    def current_name(self) -> str:
        return self.preferred or self.backend_names[0]

    def set_preferred(self, name: str) -> bool:
        """运行时切换偏好后端；preferred 失败仍按配置顺序回退。"""
        if name in self.backend_names:
            self.preferred = name
            return True
        return False

    def _ordered(self) -> list[OpenAICompatibleChatClient]:
        if not self.preferred:
            return list(self.backends)
        preferred = [b for b in self.backends if getattr(b, "name", "") == self.preferred]
        rest = [b for b in self.backends if getattr(b, "name", "") != self.preferred]
        return preferred + rest

    def complete(self, messages: list[dict[str, str]], system: str | None = None) -> str:
        ordered = self._ordered()
        last_error: Exception | None = None
        for i, backend in enumerate(ordered):
            try:
                return backend.complete(messages, system=system)
            except Exception as exc:
                last_error = exc
                remaining = len(ordered) - i - 1
                if remaining:
                    logger.warning(
                        "Chat 后端 %s 失败，切换下一个（剩 %d 个）: %s",
                        getattr(backend, "name", backend.base_url), remaining, exc,
                    )
                else:
                    logger.warning("Chat 后端 %s 失败（已是最后一个）: %s", getattr(backend, "name", backend.base_url), exc)
        if last_error:
            raise last_error
        raise RuntimeError("Chat 请求失败")

    def complete_stream(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        """按偏好顺序流式调用后端；某后端流式失败自动切下一个，全部失败回退非流式。"""
        last_error: Exception | None = None
        for backend in self._ordered():
            try:
                return backend.complete_stream(messages, system=system, on_delta=on_delta)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Chat 流式后端 %s 失败，切换下一个: %s",
                    getattr(backend, "name", backend.base_url), exc,
                )
        if last_error:
            logger.warning("所有后端流式失败，回退非流式: %s", last_error)
        return self.complete(messages, system=system)


def _build_backend(entry: dict[str, Any], default_max_tokens: int, default_timeout: float) -> OpenAICompatibleChatClient:
    api_key = str(entry.get("api_key", "") or "").strip()
    if not api_key:
        env_name = entry.get("api_key_env") or ""
        api_key = os.environ.get(env_name, "").strip() if env_name else ""
    client = OpenAICompatibleChatClient(
        base_url=str(entry.get("base_url", "")),
        model=str(entry.get("model", "")),
        api_key=api_key,
        fallback_model=str(entry.get("fallback_model", "") or ""),
        max_tokens=int(entry.get("max_tokens", default_max_tokens)),
        timeout=float(entry.get("timeout", default_timeout)),
    )
    client.name = str(entry.get("name", entry.get("model", "")))  # type: ignore[attr-defined]
    return client


def build_chat_client(chat_cfg: dict[str, Any]) -> OpenAICompatibleChatClient | MultiBackendChatClient | DisabledChatClient:
    if not chat_cfg.get("enabled", False):
        return DisabledChatClient()

    backends_cfg = chat_cfg.get("backends")
    if isinstance(backends_cfg, list) and backends_cfg:
        max_tokens = int(chat_cfg.get("max_tokens", 256))
        timeout = float(chat_cfg.get("timeout", 60))
        backends = [
            _build_backend(entry, max_tokens, timeout)
            for entry in backends_cfg
            if isinstance(entry, dict) and entry.get("base_url") and entry.get("model")
        ]
        if not backends:
            logger.warning("chat.backends 配置了但没有任何有效条目（缺 base_url/model）")
            return DisabledChatClient()
        return MultiBackendChatClient(backends)

    # 旧版单后端配置兼容
    api_key = resolve_chat_api_key(
        chat_cfg.get("api_key"),
        chat_cfg.get("api_key_env", "OPENAI_API_KEY"),
    )
    if not api_key:
        logger.warning("Chat 已启用但未找到 API Key")
        return DisabledChatClient()

    return OpenAICompatibleChatClient(
        base_url=chat_cfg.get("base_url", "https://api.codexzh.com/v1"),
        model=chat_cfg.get("model", "gpt-5.4-mini"),
        api_key=api_key,
        fallback_model=chat_cfg.get("fallback_model", "gpt-5.4"),
        max_tokens=int(chat_cfg.get("max_tokens", 256)),
        timeout=float(chat_cfg.get("timeout", 60)),
    )
