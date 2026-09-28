"""离屏渲染悬浮球 4 个状态预览 PNG（浅色/深色底各一组）。

运行: .venv/Scripts/python.exe scripts/render_ball_preview.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from screen_agent.voice.ui_qt.signals import AssistantSignals  # noqa: E402
from screen_agent.voice.ui_qt.ball import BallWidget  # noqa: E402

STATES = ["idle", "listening", "processing", "speaking"]


def main() -> None:
    app = QApplication(sys.argv)
    out_dir = ROOT / "data" / "ball_preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    for theme, bg in (("light", QColor("#f5f5f7")), ("dark", QColor("#1c1c22"))):
        for i, status in enumerate(STATES):
            signals = AssistantSignals()
            ball = BallWidget(signals)
            ball.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            ball._status = status
            ball._level = 0.6 if status == "listening" else 0.0
            ball._angle = 40 + i * 90  # 每个状态错开流光相位
            ball._hover = 0.0
            # 背景板
            from PyQt6.QtGui import QPixmap, QPainter

            canvas = QPixmap(160, 160)
            canvas.fill(bg)
            ball.show()
            for _ in range(3):
                app.processEvents()
            pix = ball.grab()
            painter = QPainter(canvas)
            painter.drawPixmap((160 - pix.width()) // 2, (160 - pix.height()) // 2, pix)
            painter.end()
            out = out_dir / f"ball_{theme}_{status}.png"
            canvas.save(str(out))
            print(f"saved {out}")
            ball.hide()


if __name__ == "__main__":
    main()
