"""感知记忆实战测试：从「看到」到「记得」到「能查」的完整链路。

跑法：.venv/Scripts/python.exe scripts/live_test_perception.py

分五段：
  A UIA 真实采集（当前前台窗口，看它到底能读到什么）
  B 行为日志 + 中文全文检索（FTS5 + bigram 是否真的精准）
  C 隐私闸门（私密窗口 / 敏感内容 / 记录时段）
  D 日报统计（今天用了什么、各停留多久）
  E 汇总
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from screen_agent.capture import uia  # noqa: E402
from screen_agent.capture.privacy import PrivacyGate  # noqa: E402
from screen_agent.capture.watcher import DesktopWatcher, SightEvent  # noqa: E402
from screen_agent.memory.journal import DesktopJournal, bigram_tokens  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def ok(label: str, condition: bool, extra: str = "") -> None:
    print(f"   {'✓' if condition else '✗'} {label}" + (f"  [{extra}]" if extra else ""))
    (PASSED if condition else FAILED).append(label)


def head(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    # ---------------- A 真实采集 ----------------
    head("A UIA 真实采集（当前前台窗口）")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        ok("SQLite 编译了 FTS5", True)
    except sqlite3.OperationalError as exc:
        ok("SQLite 编译了 FTS5", False, str(exc))
    finally:
        conn.close()

    t0 = time.time()
    content = uia.read_foreground_text()
    elapsed = (time.time() - t0) * 1000
    if content is None:
        ok("UIA 读到前台窗口内容", False, "读不到")
    else:
        ok("UIA 读到前台窗口内容", bool(content.texts), f"{len(content.texts)} 条 / {elapsed:.0f} ms")
        print(f"     窗口标题：{content.title or '(空)'}")
        for item in content.texts[:6]:
            print(f"       · {item[:64]}")

    watcher = DesktopWatcher(use_uia=False)   # 先只用标题层，避免依赖前台状态
    event = watcher.poll()
    ok("观察者能产出事件", event is not None)
    if event is not None:
        print(f"     应用归类：{event.app}｜窗口：{event.window_title or '(无标题)'}")

    # ---------------- A2 OCR 兜底 ----------------
    head("A2 OCR 兜底（UIA 拿不到内容时认图）")
    from screen_agent.capture import ocr as ocr_mod
    from screen_agent.capture.watcher import extract_url

    langs = ocr_mod.languages()
    ok("系统装了 OCR 语言包", bool(langs), str(langs))
    ok("中文字间空格会被收掉", ocr_mod.clean_text("淘 保 函") == "淘保函")
    ok("英文数字不受影响",
       ocr_mod.clean_text("localhost:5199 WorkBuddy") == "localhost:5199 WorkBuddy")

    ok("能从无障碍文本揪出网址",
       extract_url(["地址和搜索栏 https://www.zhihu.com 缩放: 90%"]) == "https://www.zhihu.com")
    ok("能认出不带协议的本地地址",
       extract_url([], "淘保函 localhost:5199/users/account 订单管理")
       == "localhost:5199/users/account")

    shell_event = SightEvent(
        ts=datetime.now(), window_title="WorkBuddy", process_name="WorkBuddy.exe",
        texts=["Chrome Legacy Window"], ocr_text="申请管理 订单管理 保函管理", source="uia+ocr",
    )
    ok("UIA 只剩空壳时 digest 改用 OCR 内容", "申请管理" in shell_event.digest())

    rich_event = SightEvent(
        ts=datetime.now(), window_title="知乎", process_name="msedge.exe",
        texts=[f"正文第{i}段" for i in range(20)], ocr_text="冗余的认图结果", source="uia",
    )
    ok("UIA 有正文时不被 OCR 覆盖",
       "正文第0段" in rich_event.digest() and "冗余的认图" not in rich_event.digest())

    # ---------------- B 记录与检索 ----------------
    head("B 行为日志 + 中文全文检索")
    tmp = Path(tempfile.mkdtemp(prefix="rita-journal-"))
    journal = DesktopJournal(tmp / "desktop.db")

    base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    samples = [
        (0, "Q3 复盘会议纪要.md - 编辑器", "Code.exe",
         ["Q3 复盘会议纪要", "讨论项目排期与人力安排", "下周交付里程碑"]),
        (18, "会议纪要.docx - WPS", "wps.exe",
         ["与会人员：张三 李四", "结论：排期顺延一周"]),
        (42, "百度一下，你就知道", "msedge.exe",
         ["深圳天气 未来一周", "周末有雨 记得带伞"]),
        (70, "Agent-Rita-World - 编辑器", "Code.exe",
         ["pipeline.py 感知链路", "dedup 去重策略", "VLM 调用降本"]),
        (95, "微信", "WeChat.exe",
         ["老王：明天上午的评审改到下午三点"]),
        (130, "账单 - 某某银行", "xxxbank.exe",
         ["账户余额 12000", "密码框"]),
        (150, "记事本", "notepad.exe",
         ["sk-abcdef1234567890abcdef1234567890", "临时存的 key"]),
    ]
    for minutes, title, process, texts in samples:
        journal.record(SightEvent(
            ts=base + timedelta(minutes=minutes),
            window_title=title, process_name=process, texts=texts, source="uia",
        ))

    # 私密/敏感的两条单独处理，模拟观察者已经判定过
    journal.record(SightEvent(
        ts=base + timedelta(minutes=131), window_title="", process_name="xxxbank.exe",
        texts=[], source="title", skip_reason="私密窗口",
    ))
    ok("记录写入成功（银行那条与含密钥那条被拦）", journal.stats()["sights_total"] == 5,
       f"共 {journal.stats()['sights_total']} 条")
    ok("私密窗口就算被直接写库也进不去",
       all(row["app"] != "xxxbank" for row in journal.recent(limit=50)))

    hits = journal.search("会议")
    ok("搜「会议」命中会议相关（2 条）", len(hits) == 2, f"命中 {len(hits)} 条")

    hits = journal.search("排期")
    ok("搜「排期」命中（不该漏）", len(hits) >= 2, f"命中 {len(hits)} 条")

    hits = journal.search("天气")
    ok("搜「天气」只命中浏览器那条", len(hits) == 1, f"命中 {len(hits)} 条")

    ok("搜「不存在的东西」不误报", len(journal.search("量子计算机保修")) == 0)

    hits = journal.search("pipeline")
    ok("英文关键词也能搜", any("pipeline" in (h["digest"] or "") for h in hits))

    hits = journal.search("会议", app="编辑器")
    ok("按应用过滤生效", all("编辑器" in h["app"] for h in hits))

    recent = journal.search("改到下午")
    ok("搜长句片段能命中微信那条", len(recent) >= 1, f"命中 {len(recent)} 条")

    print("\n   bigram 切分示例：")
    print(f"     「Q3 复盘会议纪要」 → {bigram_tokens('Q3 复盘会议纪要')}")

    # ---------------- C 隐私闸门 ----------------
    head("C 隐私闸门")
    gate = PrivacyGate()

    private, reason = gate.verdict("1Password - 保险库", "1password.exe", [])
    ok("密码管理器被拦", not private, reason)

    private, reason = gate.verdict("中国建设银行 - 个人网银", "chrome.exe", [])
    ok("银行页面被拦", not private, reason)

    private, reason = gate.verdict("会议纪要.md", "Code.exe", ["sk-abcdef1234567890abcdef1234567890"])
    ok("疑似 API key 被拦", not private, reason)

    private, reason = gate.verdict("会议纪要.md", "Code.exe", ["6222 0212 3456 7890"])
    ok("卡号被拦", not private, reason)

    private, reason = gate.verdict("pipeline.py - 编辑器", "Code.exe", ["def analyze(self):"])
    ok("正常代码不被拦", private, reason or "放行")

    gated = PrivacyGate(active_hours=(1, 2))     # 只在凌晨 1-2 点记录
    private, reason = gated.verdict("随便什么", "code.exe", [])
    ok("记录时段外被拦", not private, reason)

    blocked = DesktopJournal(tmp / "blocked.db")
    blocked.record(SightEvent(ts=datetime.now(), window_title="1Password",
                              process_name="1password.exe", skip_reason="私密窗口"))
    blocked.record(SightEvent(ts=datetime.now(), window_title="账单",
                              process_name="bank.exe", skip_reason="内容疑似敏感"))
    ok("被拦下的事件完全不落库", blocked.stats()["sights_total"] == 0)

    # ---------------- D 日报 ----------------
    head("D 日报统计")
    summary = journal.day_summary()
    print(f"     日期：{summary['date']}　共 {summary['total']} 条观察")
    print(f"     时段：{summary.get('first')} — {summary.get('last')}")
    print("     应用出现次数：")
    for app, count in summary["apps"]:
        print(f"       · {app}: {count} 次")
    print("     估算停留时长：")
    for app, seconds in summary["dwell"]:
        print(f"       · {app}: {seconds // 60} 分 {seconds % 60} 秒")
    ok("日报能统计出应用分布", len(summary["apps"]) >= 4)
    ok("日报能估算停留时长", len(summary["dwell"]) >= 2)

    journal.record(SightEvent(
        ts=datetime.now() - timedelta(days=10),
        window_title="十天前的旧记录.md", process_name="Code.exe", texts=["早该过期了"],
        source="uia",
    ))
    before_purge = journal.stats()["sights_total"]
    purged = journal.purge_before(days=7)
    ok("能按天数清理过期记录", purged == 1, f"清掉 {purged} 条（清理前 {before_purge} 条）")
    ok("旧记录清掉、新记录留住", journal.stats()["sights_total"] == before_purge - 1)

    shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- E 汇总 ----------------
    head("E 汇总")
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：")
        for item in FAILED:
            print(f"  ✗ {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
