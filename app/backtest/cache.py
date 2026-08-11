# -*- coding: utf-8 -*-
"""回测K线本地缓存：BaoStock 等数据源拉取的长历史K线落 SQLite 复用，
避免重复拉取触发数据源限速；缓存按天失效（fetched_at 非当天视为过期，
防止前复权数据随除权漂移影响回测结果）

表结构独立于实时系统的 kline_daily/kline_min，互不影响
"""
import sqlite3
import threading
import time
from datetime import datetime
from typing import List, Optional

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kline_cache (
    code TEXT NOT NULL, period TEXT NOT NULL, ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (code, period, ts)
);
CREATE INDEX IF NOT EXISTS idx_kline_cache ON kline_cache(code, period);
"""


class KlineCache:
    """回测K线缓存（线程安全，WAL 模式）"""

    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()
        conn = self._connect()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------- 写入 -------------------------

    def put(self, code: str, period: str, klines: List[dict]) -> None:
        """整段K线 upsert 入缓存（重复 ts 覆盖，标记今日拉取）"""
        if not klines:
            return
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO kline_cache"
                    "(code,period,ts,open,high,low,close,volume,amount,fetched_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(code, period, k["ts"], k["open"], k["high"], k["low"],
                      k["close"], k["volume"], k["amount"], today) for k in klines])
                conn.commit()
            finally:
                conn.close()

    # ------------------------- 读取 -------------------------

    def get(self, code: str, period: str,
            start_ts: str = "", end_ts: str = "") -> Optional[List[dict]]:
        """取当天拉取的缓存K线（升序）；start_ts/end_ts 非空时要求覆盖该区间，
        缓存缺失/过期/覆盖不足均返回 None（触发重新拉取）"""
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT ts,open,high,low,close,volume,amount FROM kline_cache "
                    "WHERE code=? AND period=? AND fetched_at=? ORDER BY ts",
                    (code, period, today)).fetchall()
            finally:
                conn.close()
        if not rows:
            return None
        kl = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
               "close": r[4], "volume": r[5], "amount": r[6]} for r in rows]
        # 覆盖检查带 7 天容差：请求起点/终点是自然日，数据从相邻交易日开始，
        # 避免节假日/周末错位导致误判覆盖不足而重复拉取
        if start_ts and self._day_gap(kl[0]["ts"], start_ts) > 7:
            return None
        if end_ts and self._day_gap(end_ts, kl[-1]["ts"]) > 7:
            return None
        return kl

    @staticmethod
    def _day_gap(a: str, b: str) -> int:
        """两个 ts（可能含时分）的自然日差，解析失败返回 0"""
        try:
            return (datetime.strptime(a[:10], "%Y-%m-%d")
                    - datetime.strptime(b[:10], "%Y-%m-%d")).days
        except ValueError:
            return 0

    def clear(self, code: str = "", period: str = "") -> int:
        """清理缓存（可指定股票/周期），返回删除条数"""
        sql = "DELETE FROM kline_cache WHERE 1=1"
        params: list = []
        if code:
            sql += " AND code=?"
            params.append(code)
        if period:
            sql += " AND period=?"
            params.append(period)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
