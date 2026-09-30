"""屏幕 OCR 兜底：UIA 读不到内容时，看一眼屏幕认字。

对标 Screenpipe 的 OCR fallback 路线。为什么要它——实测结论：

| 应用类型 | 例子 | UIA |
| --- | --- | --- |
| 原生 Win32 | 记事本、资源管理器 | ✅ 完整 |
| 浏览器 | Edge / Chrome | ✅ 完整 |
| **Electron / Chromium** | **VS Code、WorkBuddy、QQ NT、新版微信** | ❌ 只拿到一个空壳 |

Electron 系默认不暴露无障碍树，UIA 这条路对它们就是死的。认图是唯一的办法。

用 Windows 自带的 `Windows.Media.Ocr`：

- **零额外依赖**：系统自带引擎，中文语言包（`zh-Hans-CN`）随系统
- **够快**：实测 1920x1080 全屏截图 66 ms + 识别 304 ms，比 UIA 的 1.1 秒还快
- **离线**：不出本机

**中文有个必须处理的坑**：Windows OCR 逐个字输出，字与字之间会塞空格——
「淘保函」认出来是「淘 保 函」。直接入库的话，事后搜「淘保函」一条都搜不到。
所以识别完先把中文字符之间的空格收掉。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import sys
from functools import lru_cache

logger = logging.getLogger(__name__)

# 中文字符之间的空格：Windows OCR 逐字输出时会有
_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])")

# 单线程池跑协程：观察线程里本来没有事件循环，但 Qt 主线程里可能有，
# 直接 asyncio.run 会撞上「loop already running」
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")


def _sync(coro):  # noqa: ANN001
    return _EXECUTOR.submit(asyncio.run, coro).result()


@lru_cache(maxsize=1)
def _engine():  # noqa: ANN202
    """拿一个 OCR 引擎；系统没装语言包时返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        logger.debug("没有 winrt 运行库，OCR 不可用")
        return None

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is not None:
        return engine
    # 用户配置语言没有引擎时，退到系统支持的第一个（中文机器上通常是 zh-Hans-CN）
    for item in OcrEngine.available_recognizer_languages:
        try:
            engine = OcrEngine.try_create_from_language(Language(item.language_tag))
        except Exception:  # noqa: BLE001
            continue
        if engine is not None:
            return engine
    logger.debug("系统里没有可用的 OCR 语言包")
    return None


def available() -> bool:
    return _engine() is not None


def languages() -> list[str]:
    """系统装了哪些 OCR 语言包。"""
    if sys.platform != "win32":
        return []
    try:
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        return []
    try:
        return [item.language_tag for item in OcrEngine.available_recognizer_languages]
    except Exception:  # noqa: BLE001
        return []


def clean_text(text: str) -> str:
    """收掉中文字之间的空格——不收的话「淘保函」会存成「淘 保 函」，事后搜不到。"""
    if not text:
        return ""
    return _CJK_GAP.sub("", text)


async def _recognize(engine, bitmap):  # noqa: ANN001
    return await engine.recognize_async(bitmap)


def recognize_image(image) -> str:  # noqa: ANN001 - PIL.Image.Image
    """对一张 PIL 图片做 OCR，失败返回空串（绝不抛给调用方）。"""
    engine = _engine()
    if engine is None:
        return ""
    try:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
    except ImportError:
        return ""
    try:
        rgba = image.convert("RGBA")
        writer = DataWriter()
        writer.write_bytes(rgba.tobytes())
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.RGBA8, rgba.width, rgba.height
        )
        result = _sync(_recognize(engine, bitmap))
        return clean_text(getattr(result, "text", "") or "")
    except Exception:  # noqa: BLE001 - 认图失败不该影响观察循环
        logger.debug("OCR 识别失败", exc_info=True)
        return ""


def capture_and_recognize(monitor: int = 1) -> str:
    """截当前屏幕并 OCR。monitor=0 是全部屏幕拼合，1 是主屏。"""
    if sys.platform != "win32":
        return ""
    try:
        from mss import mss
    except ImportError:
        return ""
    try:
        with mss() as sct:
            shot = sct.grab(sct.monitors[monitor])
        from PIL import Image

        image = Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception:  # noqa: BLE001
        logger.debug("截图失败", exc_info=True)
        return ""
    return recognize_image(image)
