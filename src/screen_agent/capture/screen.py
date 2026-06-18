from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import mss
from PIL import Image


@dataclass
class ScreenshotFrame:
    path: Path
    captured_at: datetime
    monitor_index: int = 0


class ScreenCapturer:
    """定时采集用户屏幕快照。"""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def capture(self, monitor_index: int = 0) -> ScreenshotFrame:
        now = datetime.now()
        filename = now.strftime("%Y%m%d_%H%M%S_%f") + ".png"
        path = self.output_dir / filename

        with mss.mss() as sct:
            monitors = sct.monitors
            idx = min(monitor_index + 1, len(monitors) - 1)
            shot = sct.grab(monitors[idx])
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            img.save(path)

        return ScreenshotFrame(path=path, captured_at=now, monitor_index=monitor_index)
