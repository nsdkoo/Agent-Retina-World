"""桌面行为日志：把你一天做过的事，存成能搜的时间线。

对标 Screenpipe 的本地 SQLite + FTS5 方案，两张表：

- `sights`：每次「窗口切换」一条——时间、应用、窗口标题、当时屏幕上抓到的文本
- `sights_fts`：FTS5 全文索引

**中文为什么要先做 bigram**：FTS5 默认的 unicode61 分词器把中文按单字切，
搜「会议」会命中所有含「会」或「议」的文本，等于没过滤。bigram 之后
「会议纪要」被存成「会议 议纪 纪要」，搜「会议」才精准命中，搜「纪要」也命中。

为什么单独一张表而不复用 events：events 存的是**聚合后的活动**（同一场景合并成一条），
sights 存的是**原始观察**。聚合会把「上午看了哪几个网页」压成「上午在浏览器」，
而细节恰恰是事后回忆时最值钱的部分。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from pathlib import Path

from screen_agent.capture.privacy import PrivacyGate
from screen_agent.capture.watcher import SightEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    app          TEXT DEFAULT '',
    window_title TEXT DEFAULT '',
    process_name TEXT DEFAULT '',
    source       TEXT DEFAULT 'title',
    skip_reason  TEXT DEFAULT '',
    digest       TEXT DEFAULT '',
    windows      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sights_ts ON sights(ts);
CREATE INDEX IF NOT EXISTS idx_sights_app ON sights(app);

CREATE VIRTUAL TABLE IF NOT EXISTS sights_fts USING fts5(
    tokens,
    sight_id UNINDEXED,
    tokenize='unicode61'
);
"""

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.+#\-]+|[\u4e00-\u9fff]+")


def bigram_tokens(text: str) -> str:
    """中文切 bigram、英文按词，拼成 FTS5 能吃的 token 串。"""
    out: list[str] = []
    for token in _TOKEN_RE.findall(text or ""):
        if token[0].isascii():
            out.append(token.lower())
            continue
        if len(token) == 1:
            out.append(token)
            continue
        out.extend(token[i : i + 2] for i in range(len(token) - 1))
    return " ".join(out)


def _match_expr(query: str) -> str:
    tokens = bigram_tokens(query).split()
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


class DesktopJournal:
    """桌面观察的落库与检索。"""

    def __init__(self, db_path: Path, privacy: PrivacyGate | None = None) -> None:
        self.db_path = Path(db_path)
        self.privacy = privacy or PrivacyGate()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        """连接上下文：Windows 上 `with sqlite3.connect(...)` 只提交事务不关连接，
        库文件会被锁住，必须显式 close。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- 写 ----

    def record(self, event: SightEvent) -> int:
        """落库前最后一道闸。

        被隐私闸门挡下的事件**完全不落盘**——连「几点用过什么应用」这样的骨架都不留：
        银行与密码管理器这类，留下使用时刻本身就是泄露。

        这里还会把私密窗口与敏感内容再查一遍，不指望上游一定查过：
        落盘点是防线的最末端，绕过它的路径早晚会出现，所以它自己得站得住。
        """
        if not event.recorded:
            return 0
        if self.privacy.is_private_window(event.window_title, event.process_name):
            return 0
        if event.texts and self.privacy.looks_sensitive(" ".join(event.texts[:50])):
            return 0
        digest = event.digest()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sights (ts, app, window_title, process_name, source,"
                " skip_reason, digest, windows) VALUES (?,?,?,?,?,?,?,?)",
                (
                    event.ts.isoformat(), event.app, event.window_title,
                    event.process_name, event.source, event.skip_reason, digest,
                    "\t".join(event.windows),
                ),
            )
            sight_id = int(cursor.lastrowid or 0)
            tokens = bigram_tokens(
                f"{event.app} {event.window_title} {' '.join(event.windows)} {digest}"
            )
            if tokens:
                conn.execute(
                    "INSERT INTO sights_fts (tokens, sight_id) VALUES (?, ?)",
                    (tokens, sight_id),
                )
            return sight_id

    # ---- 读 ----

    def search(
        self,
        query: str,
        app: str = "",
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """按内容找：支持中文关键词、应用过滤、时间下限。"""
        expr = _match_expr(query)
        if not expr:
            return []
        sql = (
            "SELECT s.* FROM sights_fts f JOIN sights s ON s.id = f.sight_id "
            "WHERE sights_fts MATCH ?"
        )
        params: list = [expr]
        if app:
            sql += " AND s.app LIKE ?"
            params.append(f"%{app}%")
        if since is not None:
            sql += " AND s.ts >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY s.ts DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    def timeline(self, day: date_cls | None = None, limit: int = 300) -> list[dict]:
        target = day or datetime.now().date()
        start = datetime.combine(target, time.min).isoformat()
        end = datetime.combine(target, time.max).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sights WHERE ts BETWEEN ? AND ? ORDER BY ts ASC LIMIT ?",
                (start, end, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sights ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def day_summary(self, day: date_cls | None = None) -> dict:
        """今天都干了什么：用了哪些应用、各自停留多久、记了多少条。"""
        rows = self.timeline(day, limit=5000)
        if not rows:
            return {"date": str(day or datetime.now().date()), "total": 0, "apps": [], "dwell": []}

        dwell: dict[str, int] = {}
        counts: dict[str, int] = {}
        for index, row in enumerate(rows):
            app = row["app"] or "未知"
            counts[app] = counts.get(app, 0) + 1
            nxt = rows[index + 1] if index + 1 < len(rows) else None
            if nxt is None:
                continue
            try:
                gap = (
                    datetime.fromisoformat(nxt["ts"]) - datetime.fromisoformat(row["ts"])
                ).total_seconds()
            except ValueError:
                continue
            if 0 < gap < 3600:   # 超过一小时的间隔算离开，不计入停留
                dwell[app] = dwell.get(app, 0) + int(gap)

        return {
            "date": rows[0]["ts"][:10],
            "total": len(rows),
            "apps": sorted(counts.items(), key=lambda kv: -kv[1]),
            "dwell": sorted(dwell.items(), key=lambda kv: -kv[1]),
            "first": rows[0]["ts"][11:16],
            "last": rows[-1]["ts"][11:16],
        }

    def stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM sights").fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM sights WHERE ts >= ?",
                (datetime.combine(datetime.now().date(), time.min).isoformat(),),
            ).fetchone()[0]
        return {"sights_total": total, "sights_today": today}

    def purge_before(self, days: int) -> int:
        """删掉 N 天前的记录——常驻记录必须有自动过期，否则磁盘只涨不落。"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM sights WHERE ts < ?", (cutoff,)
            ).fetchall()]
            if not ids:
                return 0
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM sights_fts WHERE sight_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM sights WHERE id IN ({marks})", ids)
            return len(ids)
