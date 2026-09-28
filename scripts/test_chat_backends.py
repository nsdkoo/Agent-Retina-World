"""chat.backends 连通性测试：逐个后端发一条 5 token 请求。

运行: .venv/Scripts/python.exe scripts/test_chat_backends.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from screen_agent.config import load_yaml  # noqa: E402
from screen_agent.understand.chat import (  # noqa: E402
    MultiBackendChatClient,
    _build_backend,
)


def main() -> int:
    cfg = load_yaml(ROOT / "config.yaml").get("chat", {})
    backends_cfg = cfg.get("backends", [])
    if not backends_cfg:
        print("config.yaml 里没有 chat.backends")
        return 1

    results: list[tuple[str, str, float]] = []
    for entry in backends_cfg:
        name = entry.get("name", "?")
        client = _build_backend(entry, int(cfg.get("max_tokens", 256)), float(cfg.get("timeout", 60)))
        t0 = time.time()
        try:
            reply = client.complete(
                [{"role": "user", "content": "回复「你好」两个字"}],
                system="你是一个测试桩，只回复两个字。",
            )
            dt = time.time() - t0
            print(f"[OK]   {name:20s} {dt:5.2f}s  回复: {reply[:30]}")
            results.append((name, "ok", dt))
        except Exception as exc:
            dt = time.time() - t0
            print(f"[FAIL] {name:20s} {dt:5.2f}s  {str(exc)[:120]}")
            results.append((name, f"fail: {str(exc)[:60]}", dt))

    multi = MultiBackendChatClient(
        [_build_backend(e, 256, 60) for e in backends_cfg if e.get("base_url") and e.get("model")]
    )
    print(f"\n回退链测试（首选 {multi.current_name}）:")
    reply = multi.complete([{"role": "user", "content": "1+1=? 只回答数字"}])
    print(f"  回复: {reply[:30]}")

    failed = [r for r in results if r[1] != "ok"]
    if failed:
        print(f"\n{len(failed)} 个后端不可用（回退链仍工作）")
    else:
        print("\n全部后端可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
