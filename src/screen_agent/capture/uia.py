"""Windows UI Automation 结构化读取：拿「屏幕上有什么字」，不用截图。

对标 Screenpipe 的做法——**accessibility tree 优先，截图/OCR 兜底**。
读屏软件用的就是这套 API，能直接拿到控件的 Name 与 Value：

- 比「截图 → VLM 识图」便宜一到两个数量级，也不吃显卡
- 拿到的是**精确文本**，不是识别结果，不会把 `l` 认成 `1`
- 对浏览器、Office、大部分原生应用都能拿到正文内容

实现走 PowerShell + .NET 自带的 `UIAutomationClient`：Windows 上零依赖，
不用额外装 COM 封装。起一次进程几百毫秒——因为只在窗口切换时才调用，
这点开销完全划算；而定时截图那条路是每秒都在烧。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_UIA_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$hwndValue = __HWND__
if ($hwndValue -ne 0) {
    # 直接用前台窗口句柄取根元素。别从 FocusedElement 往上找——焦点落在任务栏或
    # 开始菜单时，往上找会一路爬到任务栏去，结果抓回来一排「开始 / 搜索 / 任务视图」
    $top = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new($hwndValue))
} else {
    $fg = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $fg) { exit 0 }
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $top = $fg
    for ($i = 0; $i -lt 24; $i++) {
        $parent = $walker.GetParent($top)
        if ($null -eq $parent) { break }
        $top = $parent
    }
}
if ($null -eq $top) { exit 0 }

$out = New-Object System.Collections.Generic.List[string]
if ($top.Current.Name) { $out.Add("WINDOW`t" + $top.Current.Name) }
if ($top.Current.ProcessId) { $out.Add("PID`t" + $top.Current.ProcessId) }

$all = $top.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition)

$limit = $all.Count
if ($limit -gt 500) { $limit = 500 }
$vpPattern = [System.Windows.Automation.ValuePattern]::Pattern
for ($i = 0; $i -lt $limit; $i++) {
    $e = $all.Item($i)
    $name = $e.Current.Name
    if ($name -and $name.Trim().Length -gt 1) { $out.Add("TEXT`t" + $name.Trim()) }
    try {
        $vp = $e.GetCurrentPattern($vpPattern)
        if ($null -ne $vp) {
            $val = $vp.Current.Value
            if ($val -and $val.Trim().Length -gt 1) { $out.Add("VALUE`t" + $val.Trim()) }
        }
    } catch { }
}
$out | Select-Object -Unique
"""


@dataclass
class WindowContent:
    """当前窗口的结构化内容。texts 是屏幕上真实出现的字，不是识别出来的。"""

    title: str = ""
    texts: list[str] = field(default_factory=list)
    pid: int = 0
    source: str = "uia"

    def digest(self, limit: int = 40) -> str:
        """压成一行摘要，用于时间线与检索。"""
        parts = [t for t in self.texts if t.strip()][:limit]
        return " ".join(parts)[:600]


def available() -> bool:
    return sys.platform == "win32"


def foreground_hwnd() -> int:
    """当前前台窗口句柄；拿不到返回 0。"""
    if not available():
        return 0
    import ctypes

    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except OSError:
        return 0


def read_foreground_text(
    timeout: float = 8.0,
    max_texts: int = 200,
    min_length: int = 2,
    hwnd: int = 0,
) -> WindowContent | None:
    """读当前前台窗口的结构化文本。hwnd=0 时自己去取前台句柄。"""
    if not available():
        return None
    target = int(hwnd) or foreground_hwnd()
    script = _UIA_SCRIPT.replace("__HWND__", str(target))
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("UIA 读取失败或超时", exc_info=True)
        return None

    raw = proc.stdout or b""
    for encoding in ("utf-8", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    content = WindowContent()
    seen: set[str] = set()
    for line in text.splitlines():
        if "\t" not in line:
            continue
        kind, _, value = line.partition("\t")
        value = value.strip()
        if not value:
            continue
        if kind == "WINDOW":
            content.title = value
        elif kind == "PID":
            try:
                content.pid = int(value)
            except ValueError:
                pass
        elif kind in ("TEXT", "VALUE"):
            if len(value) < min_length or value in seen:
                continue
            seen.add(value)
            content.texts.append(value)
            if len(content.texts) >= max_texts:
                break

    if not content.title and not content.texts:
        return None
    return content
