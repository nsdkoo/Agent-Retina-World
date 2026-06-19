import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw

from screen_agent.activity.store import ActivityAggregator
from screen_agent.dedup.hasher import ScreenshotDeduper
from screen_agent.understand.vlm import PageCategory, PageUnderstanding, UserAction


class DedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.deduper = ScreenshotDeduper(phash_threshold=8, enable_histogram=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _save(self, name: str, variant: int) -> Path:
        path = Path(self.tmp.name) / name
        img = Image.new("RGB", (800, 600), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        if variant == 1:
            draw.rectangle([50, 50, 400, 400], fill=(10, 20, 30))
            draw.text((60, 60), "pattern-alpha", fill=(0, 0, 0))
        else:
            draw.ellipse([200, 100, 700, 500], fill=(200, 50, 80))
            draw.line([(0, 0), (800, 600)], fill=(0, 0, 255), width=8)
            draw.text((220, 120), "pattern-beta", fill=(255, 255, 255))
        img.save(path)
        return path

    def test_identical_frames_are_duplicates(self) -> None:
        p1 = self._save("a.png", 1)
        p2 = self._save("b.png", 1)
        self.assertFalse(self.deduper.check(p1).is_duplicate)
        self.assertTrue(self.deduper.check(p2).is_duplicate)

    def test_different_frames_pass(self) -> None:
        deduper = ScreenshotDeduper(phash_threshold=4, enable_histogram=False)
        p1 = self._save("a.png", 1)
        p2 = self._save("b.png", 2)
        self.assertFalse(deduper.check(p1).is_duplicate)
        self.assertFalse(deduper.check(p2).is_duplicate)


class ActivityTests(unittest.TestCase):
    def _frame(self, cat: PageCategory, action: UserAction, minute: int) -> PageUnderstanding:
        t = datetime(2026, 6, 18, 10, minute, 0)
        return PageUnderstanding(
            screenshot_path=f"/tmp/{minute}.png",
            analyzed_at=t,
            page_category=cat,
            user_action=action,
            summary=f"{cat.value}-{minute}",
        )

    def test_merge_same_activity_run(self) -> None:
        agg = ActivityAggregator(merge_window=timedelta(minutes=5))
        agg.ingest(self._frame(PageCategory.IDE, UserAction.DEBUGGING, 0))
        agg.ingest(self._frame(PageCategory.IDE, UserAction.DEBUGGING, 1))
        events = agg.ingest(self._frame(PageCategory.BROWSER, UserAction.BROWSING, 2))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].frame_count, 2)
        self.assertEqual(events[0].page_category, "ide")
        flushed = agg.flush_all()
        self.assertEqual(len(flushed), 1)
        self.assertEqual(flushed[0].page_category, "browser")


if __name__ == "__main__":
    unittest.main()
