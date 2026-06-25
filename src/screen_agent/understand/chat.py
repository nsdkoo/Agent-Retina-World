from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

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


class DisabledChatClient:
    """Chat 未启用时的占位客户端。"""

    def complete(self, messages: list[dict[str, str]], system: str | None = None) -> str:
        raise RuntimeError("Chat 未启用，请在 config.yaml 配置 chat 段并设置 API Key")


def build_chat_client(chat_cfg: dict[str, Any]) -> OpenAICompatibleChatClient | DisabledChatClient:
    if not chat_cfg.get("enabled", False):
        return DisabledChatClient()

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
