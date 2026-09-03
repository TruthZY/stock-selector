# -*- coding: utf-8 -*-
"""推送系统独立状态存储（不碰 kline.db 的业务表）

用途：
  - 幂等/去重：某作业在某交易日某周期已成功执行过 → 跳过（当日只推一次、盘后只补一次）；
  - 错过补跑：调度器启动时据此判断今日槽位是否已做；
  - 可观测：记录每次运行的状态/命中数/覆盖率/耗时说明，供排查。

表 runs 主键 (job,date,period,slot)：
  job    scan=作业A盘中扫描推送 / update=作业B盘后更新
  date   YYYY-MM-DD 交易日
  period daily/30m...
  slot   触发时刻 "14:00" / "15:40" ...
  status ok / fail / skip
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    job      TEXT NOT NULL,
    date     TEXT NOT NULL,
    period   TEXT NOT NULL,
    slot     TEXT NOT NULL,
    status   TEXT NOT NULL,
    matches  INTEGER DEFAULT 0,
    coverage REAL    DEFAULT 0,
    elapsed_ms INTEGER DEFAULT 0,
    detail   TEXT    DEFAULT '',
    ts       TEXT    NOT NULL,
    PRIMARY KEY (job, date, period, slot)
);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(date, job);

-- 命中滑动记录：每次扫描(盘前 pre / 盘后 post)命中的股票逐日留痕，
-- 用于统计"近 N 个扫描日内某股被盘前/盘后各命中几次"。
CREATE TABLE IF NOT EXISTS hits (
    session  TEXT NOT NULL,          -- pre=盘前(14:00实时) / post=盘后(21:00收盘)
    date     TEXT NOT NULL,          -- YYYY-MM-DD 交易日
    period   TEXT NOT NULL,          -- daily/30m...
    code     TEXT NOT NULL,
    name     TEXT DEFAULT '',
    rank     INTEGER DEFAULT 0,      -- 当次质量排名(1起)
    n_conditions INTEGER DEFAULT 0,
    quality  REAL DEFAULT 0,
    ts       TEXT NOT NULL,
    PRIMARY KEY (session, date, period, code)
);
CREATE INDEX IF NOT EXISTS idx_hits_session_date ON hits(session, period, date);
"""


class State:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.db_path = os.path.join(state_dir, "push.db")
        self._lock = threading.RLock()
        conn = self._connect()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # --- 幂等判定 ---
    def is_done(self, job: str, date: str, period: str,
                slot: Optional[str] = None, status: Optional[str] = "ok") -> bool:
        """该作业今日该周期是否已执行到指定状态。

        status="ok"：只看成功的（作业B阶梯重试用——今日已成功就跳过后续阶梯）；
        status=None ：任意状态都算（"该槽位是否已尝试过"，用于每槽只跑一次的幂等，
                      避免失败/降级后每个 tick 反复重跑刷屏）；
        slot=None   ：跨所有槽位；slot 指定：只看该槽位。
        """
        sql = "SELECT 1 FROM runs WHERE job=? AND date=? AND period=?"
        params: list = [job, date, period]
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        if slot is not None:
            sql += " AND slot=?"
            params.append(slot)
        sql += " LIMIT 1"
        with self._lock:
            conn = self._connect()
            try:
                return conn.execute(sql, params).fetchone() is not None
            finally:
                conn.close()

    def mark(self, job: str, date: str, period: str, slot: str, status: str,
             matches: int = 0, coverage: float = 0.0, elapsed_ms: int = 0,
             detail: str = "") -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO runs"
                    "(job,date,period,slot,status,matches,coverage,elapsed_ms,detail,ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (job, date, period, slot, status, int(matches), float(coverage),
                     int(elapsed_ms), detail[:500], time.strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            finally:
                conn.close()

    def recent(self, n: int = 20) -> List[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT job,date,period,slot,status,matches,coverage,elapsed_ms,detail,ts "
                    "FROM runs ORDER BY ts DESC LIMIT ?", (int(n),)).fetchall()
            finally:
                conn.close()
        return [{"job": r[0], "date": r[1], "period": r[2], "slot": r[3],
                 "status": r[4], "matches": r[5], "coverage": r[6],
                 "elapsed_ms": r[7], "detail": r[8], "ts": r[9]} for r in rows]

    # --- 命中滑动记录（盘前 pre / 盘后 post）---
    def record_hits(self, session: str, date: str, period: str,
                    matches: List[dict]) -> None:
        """记录一次扫描的全部命中到滑动记录。

        同日同 session 覆盖（先删后插）→ 幂等：手动重跑/补跑不会重复累加。
        记录的是"扫中的全部"（result.matches 完整清单），不是只记 top_n。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM hits WHERE session=? AND date=? AND period=?",
                             (session, date, period))
                for rank, m in enumerate(matches, 1):
                    conn.execute(
                        "INSERT OR REPLACE INTO hits"
                        "(session,date,period,code,name,rank,n_conditions,quality,ts) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (session, date, period, str(m.get("code", "")),
                         str(m.get("name", "")), rank,
                         int(m.get("n_conditions", 0) or 0),
                         float(m.get("quality", 0.0) or 0.0), now))
                conn.commit()
            finally:
                conn.close()

    def hit_counts(self, session: str, period: str, codes: List[str],
                   window_days: int = 10) -> dict:
        """近 window_days 个"扫描日"内每只 code 的命中次数。

        窗口按该 session 最近 N 个**有记录的交易日**算（自然跳过周末/节假日），
        而非 N 个自然日——这样"10天"= 最近10次扫描，语义更贴合盘感。
        返回 {code: count}，未命中的 code 计 0。
        """
        codes = [str(c) for c in codes]
        if not codes:
            return {}
        with self._lock:
            conn = self._connect()
            try:
                dates = [r[0] for r in conn.execute(
                    "SELECT DISTINCT date FROM hits WHERE session=? AND period=? "
                    "ORDER BY date DESC LIMIT ?",
                    (session, period, int(window_days))).fetchall()]
                if not dates:
                    return {c: 0 for c in codes}
                marks = ",".join("?" * len(dates))
                rows = conn.execute(
                    f"SELECT code, COUNT(*) FROM hits WHERE session=? AND period=? "
                    f"AND date IN ({marks}) GROUP BY code",
                    [session, period, *dates]).fetchall()
            finally:
                conn.close()
        cnt = {r[0]: int(r[1]) for r in rows}
        return {c: cnt.get(c, 0) for c in codes}

    def prune_hits(self, keep_dates: int = 40) -> None:
        """滑动修剪：每个 (session,period) 只保留最近 keep_dates 个交易日的记录，
        防止 hits 表无限增长。keep_dates 应 > window_days。"""
        with self._lock:
            conn = self._connect()
            try:
                pairs = conn.execute(
                    "SELECT DISTINCT session, period FROM hits").fetchall()
                for session, period in pairs:
                    keep = [r[0] for r in conn.execute(
                        "SELECT DISTINCT date FROM hits WHERE session=? AND period=? "
                        "ORDER BY date DESC LIMIT ?",
                        (session, period, int(keep_dates))).fetchall()]
                    if not keep:
                        continue
                    cutoff = min(keep)
                    conn.execute(
                        "DELETE FROM hits WHERE session=? AND period=? AND date < ?",
                        (session, period, cutoff))
                conn.commit()
            finally:
                conn.close()
