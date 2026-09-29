"""GUI Agent Loop：截图 → 多模态 VLM 输出动作 JSON → SendInput 执行 → 复验截图。

UFO²/Agent S3 的最小闭环离线版：
- 感知：PIL ImageGrab 截主屏（base64）
- 决策：多模态 VLM 按严格动作 JSON 输出（模型提议）
- 执行：tools.input 的 SendInput 原语（registry HIGH 确认钩子把关）
- 终止：done / fail / 步数上限；坐标漂移不重试乱点
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

from screen_agent.tools.registry import ToolRegistry
from screen_agent.voice.executor import ActionResult

VALID_ACTIONS = {"click", "double_click", "right_click", "type", "key", "scroll", "done", "fail"}

PROMPT_TEMPLATE = """你是 Windows 桌面 GUI 操控助手。看截图，按用户指令决定下一个动作。

用户指令：{instruction}

屏幕分辨率：{width}x{height}。坐标必须用这个分辨率下的像素值。
{history_block}
动作 JSON 格式（只输出一个 JSON 对象，不要多余文字）：
- {{"action": "click", "x": <int>, "y": <int>, "thought": "<为什么点这里>"}}
- {{"action": "double_click", "x": <int>, "y": <int>, "thought": "..."}}
- {{"action": "right_click", "x": <int>, "y": <int>, "thought": "..."}}
- {{"action": "type", "text": "<要输入的内容>", "thought": "..."}}
- {{"action": "key", "key": "enter|esc|tab|delete", "thought": "..."}}
- {{"action": "scroll", "delta": <int 正上负下>, "thought": "..."}}
- {{"action": "done", "thought": "<完成依据>"}}
- {{"action": "fail", "thought": "<为什么做不到>"}}

原则：
1. 优先非 GUI 方式：任务若用打开网址/打开应用能完成，返回 fail 并在 thought 里说明建议。
2. 点击前确认目标元素在截图中确实可见。
3. 任务完成立即 done，不要多余动作。
"""


@dataclass
class GuiAction:
    action: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    key: str | None = None
    delta: int | None = None
    thought: str = ""
    raw: dict = field(default_factory=dict)


def _parse_action(content: str) -> GuiAction | None:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action", "")).lower()
    if action not in VALID_ACTIONS:
        return None
    return GuiAction(
        action=action,
        x=data.get("x"),
        y=data.get("y"),
        text=data.get("text"),
        key=data.get("key"),
        delta=data.get("delta"),
        thought=str(data.get("thought", "")),
        raw=data,
    )


class GuiAgent:
    def __init__(
        self,
        registry: ToolRegistry,
        base_url: str,
        model: str,
        api_key: str,
        max_steps: int = 5,
        timeout: float = 120.0,
    ) -> None:
        self.registry = registry
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_steps = max_steps
        self.timeout = timeout

    # ---- 感知 ----

    def _screenshot_b64(self) -> tuple[str, int, int]:
        from PIL import ImageGrab

        image = ImageGrab.grab()
        width, height = image.size
        import io

        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return base64.b64encode(buffer.getvalue()).decode(), width, height

    def _vision_complete(self, b64: str, prompt: str) -> str:
        import httpx

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": 512,
        }
        resp = httpx.Client(timeout=self.timeout).post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    # ---- 执行 ----

    def _execute(self, action: GuiAction) -> ActionResult:
        mapping = {
            "click": ("input.mouse_click", {"x": action.x, "y": action.y}),
            "double_click": ("input.mouse_click", {"x": action.x, "y": action.y, "double": True}),
            "right_click": ("input.mouse_click", {"x": action.x, "y": action.y, "button": "right"}),
            "type": ("input.type_text", {"text": action.text or ""}),
            "key": ("input.press_key", {"key": action.key or ""}),
            "scroll": ("input.scroll", {"delta": int(action.delta or 0)}),
        }
        if action.action not in mapping:
            return ActionResult(success=False, message=f"不可执行的动作：{action.action}")
        name, params = mapping[action.action]
        return self.registry.run(name, **params)

    def run(self, instruction: str) -> ActionResult:
        if not instruction:
            return ActionResult(success=False, message="指令是空的")
        history: list[str] = []
        for _step in range(1, self.max_steps + 1):
            b64, width, height = self._screenshot_b64()
            history_block = ""
            if history:
                history_block = "已执行的动作：\n" + "\n".join(f"- {h}" for h in history) + "\n\n根据最新截图判断下一步。"
            prompt = PROMPT_TEMPLATE.format(
                instruction=instruction, width=width, height=height, history_block=history_block
            )
            try:
                content = self._vision_complete(b64, prompt)
            except Exception as exc:  # noqa: BLE001
                return ActionResult(success=False, message=f"视觉模型不可用：{exc}")
            action = _parse_action(content)
            if action is None:
                return ActionResult(success=False, message="视觉模型输出无法解析，已停止（不盲执行）")

            if action.action == "done":
                return ActionResult(success=True, message=f"完成：{action.thought}")
            if action.action == "fail":
                return ActionResult(success=False, message=f"做不到：{action.thought}")

            result = self._execute(action)
            record = f"{action.action}({action.thought or ''}) → {'成功' if result.success else '失败'}"
            history.append(record)
            if not result.success:
                return ActionResult(success=False, message=f"动作执行失败：{result.message}")
        return ActionResult(
            success=False,
            message=f"已达最大步数（{self.max_steps}）还没完成，已停止避免乱点",
        )
