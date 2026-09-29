"""意图解析评测基准：标注集 → 准确率报告（防止改正则时静默退化）。

用法：python benchmarks/intents_eval.py
标注集覆盖：直连指令 / 别名 / 工具意图 / 兜底 chat / 易混淆边界。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.voice.intents import IntentType, parse_intent

# (话术, 期望意图, 期望 tool/空)  —— 期望 tool 的用 tool 路由断言
CASES: list[tuple[str, IntentType, str]] = [
    ("帮我截个图", IntentType.SCREENSHOT, ""),
    ("截屏", IntentType.SCREENSHOT, ""),
    ("分析一下屏幕", IntentType.ANALYZE_SCREEN, ""),
    ("打开百度", IntentType.OPEN_URL, ""),
    ("打开github.com", IntentType.OPEN_URL, ""),
    ("打开qq", IntentType.OPEN_APP, ""),
    ("打开微信", IntentType.OPEN_APP, ""),
    ("退出微信", IntentType.CLOSE_APP, "app.close"),
    ("关闭网易云音乐", IntentType.CLOSE_APP, "app.close"),
    ("音量调大一点", IntentType.VOLUME, "volume.up"),
    ("音量小一点", IntentType.VOLUME, "volume.down"),
    ("静音", IntentType.VOLUME, "volume.mute"),
    ("剪贴板里有什么", IntentType.CLIPBOARD, "clip.get"),
    ("复制一下hello", IntentType.CLIPBOARD, "clip.set"),
    ("找文件周报", IntentType.FIND_FILES, "files.find"),
    ("列出打开的窗口", IntentType.LIST_WINDOWS, "win.list"),
    ("切到微信窗口", IntentType.FOCUS_WINDOW, "win.focus"),
    ("锁屏", IntentType.LOCK_SCREEN, "sys.lock"),
    ("帮我点一下确定按钮", IntentType.GUI_TASK, ""),
    ("今日总结", IntentType.DAILY_REPORT, ""),
    ("看下活动时间线", IntentType.TIMELINE, ""),
    ("退出", IntentType.END_SESSION, ""),
    ("没事了", IntentType.END_SESSION, ""),
    ("今天心情不错", IntentType.CHAT, ""),
    ("你觉得AI会取代程序员吗", IntentType.CHAT, ""),
]


def run() -> float:
    passed = 0
    failures: list[str] = []
    for text, want_type, want_tool in CASES:
        intent = parse_intent(text, {"cursor": "Cursor"}, {"百度": "https://www.baidu.com"})
        ok_type = intent.type == want_type
        ok_tool = (intent.tool == want_tool) if want_tool else True
        if ok_type and ok_tool:
            passed += 1
        else:
            failures.append(f"{text} → {intent.type.value}/{intent.tool or '-'}（期望 {want_type.value}/{want_tool or '-'}）")
    for line in failures:
        print(f"  ❌ {line}")
    score = passed / len(CASES) * 100
    print(f"意图评测：{passed}/{len(CASES)} = {score:.0f}%")
    return score


if __name__ == "__main__":
    run()
