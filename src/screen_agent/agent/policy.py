"""工具权限策略：四种模式 + 工具级覆盖 + 危险动作强制确认。

对标 block/goose 的权限体系：

- **模式**：`auto` 全自动 / `approve` 每个都问 / `smart` 只读与可逆动作自动放行 / `chat` 只用对话
- **工具级覆盖**：AlwaysAllow / AskBefore / NeverAllow，优先级高于模式，可持久化
- **危险动作**：删除、关闭应用这类不可逆操作，无论什么模式都要上报确认
- **安全默认**：`auto` 模式下高风险动作仍要确认（桌面助手会注入键鼠、删文件，
  真出事没有回滚余地），要彻底放开得显式打开 `auto_allows_high`
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path

from screen_agent.tools.registry import RiskLevel, ToolSpec

logger = logging.getLogger(__name__)


class PermissionMode(str, Enum):
    AUTO = "auto"
    APPROVE = "approve"
    SMART = "smart"
    CHAT = "chat"


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolPermission(str, Enum):
    ALWAYS_ALLOW = "always_allow"
    ASK_BEFORE = "ask_before"
    NEVER_ALLOW = "never_allow"


# 无论如何都要问一句的工具（不可逆 / 影响面大）
_DESTRUCTIVE_TOOLS = {"files.delete", "app.close", "sys.lock"}

# 参数里带这些字眼，即使是普通工具也降级为需确认
_DESTRUCTIVE_HINTS = ("删除", "删掉", "清空", "格式化", "覆盖", "卸载", "重启", "关机")

MODE_LABELS = {
    PermissionMode.AUTO: "全自动",
    PermissionMode.APPROVE: "手动批准",
    PermissionMode.SMART: "智能批准",
    PermissionMode.CHAT: "仅对话",
}


class PolicyEngine:
    """工具调用的准入门卫。judge 返回 (决策, 理由)，理由是给人看的话术。"""

    def __init__(
        self,
        mode: PermissionMode | str = PermissionMode.SMART,
        overrides: dict[str, ToolPermission] | None = None,
        store_path: Path | None = None,
        auto_allows_high: bool = False,
    ) -> None:
        self._mode = PermissionMode(mode) if not isinstance(mode, PermissionMode) else mode
        self._overrides = dict(overrides or {})
        self._store_path = store_path
        self.auto_allows_high = auto_allows_high
        if store_path is not None:
            self._load()

    # ---- 对外 ----

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    def set_mode(self, mode: PermissionMode | str) -> None:
        self._mode = PermissionMode(mode) if not isinstance(mode, PermissionMode) else mode
        self._save()

    def judge(self, spec: ToolSpec, params: dict) -> tuple[Decision, str]:
        if self._mode is PermissionMode.CHAT:
            return Decision.DENY, "当前是仅对话模式，不动手"

        override = self._overrides.get(spec.name)
        if override is ToolPermission.NEVER_ALLOW:
            return Decision.DENY, f"「{spec.name}」被设为永不允许"
        if override is ToolPermission.ALWAYS_ALLOW:
            return Decision.ALLOW, ""
        if override is ToolPermission.ASK_BEFORE:
            return Decision.ASK, f"「{spec.name}」被设为每次都要确认"

        # 命令执行单独判：shell 内部知道自己在跑什么，只读的放行、要命的直接拒，
        # 比"一刀切问一遍"更贴近 Codex 的分级做法
        if spec.name == "shell.run":
            from screen_agent.tools.shell import classify_command

            level, why = classify_command(str((params or {}).get("cmd") or ""))
            if level == "deny":
                return Decision.DENY, why
            if level == "safe":
                return Decision.ALLOW, ""
            return Decision.ASK, why

        # 参数里带危险字眼是最该警惕的一类：工具本身可能很常规，参数却指向意料之外的用法。
        # 它优先于所有模式与信任开关——信任工具不等于信任这次调用的参数。
        if self._dangerous_params(params):
            return Decision.ASK, "这一步看着不太对劲，需要你点头"

        # 显式打开的「闭眼信任」：连不可逆工具也不再问
        if self._mode is PermissionMode.AUTO and self.auto_allows_high:
            return Decision.ALLOW, ""

        if spec.name in _DESTRUCTIVE_TOOLS:
            return Decision.ASK, "这一步不可逆，需要你点头"

        if self._mode is PermissionMode.APPROVE:
            if spec.risk is RiskLevel.SAFE:
                return Decision.ALLOW, ""
            return Decision.ASK, "手动批准模式：每次动手都问你"

        if self._mode is PermissionMode.SMART:
            if spec.risk <= RiskLevel.LOW:
                return Decision.ALLOW, ""
            return Decision.ASK, "高风险动作，智能批准模式要确认"

        # AUTO
        if spec.risk is RiskLevel.HIGH and not self.auto_allows_high:
            return Decision.ASK, "高风险动作，全自动模式下也保守问一句"
        return Decision.ALLOW, ""

    def remember(self, tool_name: str, permission: ToolPermission | str) -> None:
        """把用户的「以后都允许 / 以后都别问」记下来。"""
        self._overrides[tool_name] = (
            permission if isinstance(permission, ToolPermission) else ToolPermission(permission)
        )
        self._save()

    def forget(self, tool_name: str) -> None:
        self._overrides.pop(tool_name, None)
        self._save()

    def describe(self) -> str:
        lines = [f"权限模式：{MODE_LABELS.get(self._mode, self._mode.value)}"]
        for name, perm in sorted(self._overrides.items()):
            lines.append(f"  {name} → {perm.value}")
        return "\n".join(lines)

    def snapshot(self) -> dict:
        return {
            "mode": self._mode.value,
            "auto_allows_high": self.auto_allows_high,
            "overrides": {k: v.value for k, v in self._overrides.items()},
        }

    # ---- 内部 ----

    @staticmethod
    def _dangerous_params(params: dict) -> bool:
        """参数值里蹦出「删除 / 格式化 / 覆盖」这类字眼，说明这次调用的意图可疑。"""
        blob = " ".join(str(v) for v in (params or {}).values())
        return any(hint in blob for hint in _DESTRUCTIVE_HINTS)

    def _load(self) -> None:
        if self._store_path is None or not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.debug("权限配置读取失败", exc_info=True)
            return
        if data.get("mode"):
            try:
                self._mode = PermissionMode(data["mode"])
            except ValueError:
                pass
        self.auto_allows_high = bool(data.get("auto_allows_high", self.auto_allows_high))
        for name, value in (data.get("overrides") or {}).items():
            try:
                self._overrides[name] = ToolPermission(value)
            except ValueError:
                continue

    def _save(self) -> None:
        if self._store_path is None:
            return
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.debug("权限配置写入失败", exc_info=True)
