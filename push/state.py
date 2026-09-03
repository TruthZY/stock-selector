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
