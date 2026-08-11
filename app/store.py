# -*- coding: utf-8 -*-
"""SQLite 存储层：股票池、K线、信号"""
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code    TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    market  TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS kline_daily (
    code   TEXT NOT NULL,
    ts     TEXT NOT NULL,
    open   REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (code, ts)
);
CREATE TABLE IF NOT EXISTS kline_min (
    code   TEXT NOT NULL,
    ts     TEXT NOT NULL,
    open   REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (code, ts)
);
CREATE INDEX IF NOT EXISTS idx_kline_daily ON kline_daily(code);
CREATE INDEX IF NOT EXISTS idx_kline_min ON kline_min(code);
CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT DEFAULT '',
    strategy   TEXT NOT NULL,
    price      REAL DEFAULT 0,
    change_pct REAL DEFAULT 0,
    reason     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE TABLE IF NOT EXISTS watch (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    added_at TEXT DEFAULT ''
);
"""


class Store:
    """SQLite 存储（线程安全：每个连接独立 + 写锁）"""

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

    # ------------------------- 股票池 -------------------------

    def upsert_stocks(self, stocks: List[Tuple[str, str]]) -> None:
        if not stocks:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT INTO stocks(code,name) VALUES(?,?) "
                    "ON CONFLICT(code) DO UPDATE SET name=excluded.name",
                    stocks,
                )
                conn.commit()
            finally:
                conn.close()

    def get_stocks(self, enabled_only: bool = True) -> List[Tuple[str, str]]:
        sql = "SELECT code,name FROM stocks"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._lock:
            conn = self._connect()
            try:
                return conn.execute(sql + " ORDER BY code").fetchall()
            finally:
                conn.close()

    def remove_stock(self, code: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM stocks WHERE code=?", (code,))
                conn.execute("DELETE FROM kline_daily WHERE code=?", (code,))
                conn.execute("DELETE FROM kline_min WHERE code=?", (code,))
                conn.commit()
            finally:
                conn.close()

    # ------------------------- 自选股 -------------------------

    def get_watch(self) -> List[Tuple[str, str]]:
        with self._lock:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT code,name FROM watch ORDER BY added_at, code").fetchall()
            finally:
                conn.close()

    def add_watch(self, code: str, name: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO watch(code,name,added_at) VALUES(?,?,?)",
                    (code, name, time.strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            finally:
                conn.close()

    def remove_watch(self, code: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM watch WHERE code=?", (code,))
                conn.commit()
            finally:
                conn.close()

    # ------------------------- K线 -------------------------

    def upsert_klines(self, code: str, klines: List[dict], table: str = "kline_daily") -> int:
        """增量写入K线，返回实际写入条数"""
        if not klines:
            return 0
        assert table in ("kline_daily", "kline_min")
        with self._lock:
            conn = self._connect()
            try:
                existing = {r[0] for r in conn.execute(
                    f"SELECT ts FROM {table} WHERE code=?", (code,))}
                rows = [
                    (code, k["ts"], k["open"], k["high"], k["low"], k["close"],
                     k["volume"], k["amount"])
                    for k in klines if k["ts"] not in existing
                ]
                if rows:
                    conn.executemany(
                        f"INSERT OR IGNORE INTO {table}(code,ts,open,high,low,close,volume,amount) "
                        f"VALUES(?,?,?,?,?,?,?,?)", rows)
                    conn.commit()
                return len(rows)
            finally:
                conn.close()

    def get_klines(self, code: str, table: str = "kline_daily",
                   limit: int = 300) -> List[dict]:
        assert table in ("kline_daily", "kline_min")
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT ts,open,high,low,close,volume,amount FROM {table} "
                    f"WHERE code=? ORDER BY ts DESC LIMIT ?", (code, limit)).fetchall()
            finally:
                conn.close()
        out = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                "close": r[4], "volume": r[5], "amount": r[6]} for r in rows]
        out.reverse()
        return out

    def last_kline_ts(self, code: str, table: str = "kline_daily") -> Optional[str]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT MAX(ts) FROM {table} WHERE code=?", (code,)).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    # ------------------------- 信号 -------------------------

    def add_signal(self, code: str, name: str, strategy: str,
                   price: float, change_pct: float, reason: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO signals(ts,code,name,strategy,price,change_pct,reason) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), code, name, strategy,
                     price, change_pct, reason))
                conn.execute("DELETE FROM signals WHERE id NOT IN "
                             "(SELECT id FROM signals ORDER BY id DESC LIMIT 500)")
                conn.commit()
            finally:
                conn.close()

    def get_signals(self, limit: int = 100, code: Optional[str] = None) -> List[dict]:
        sql = "SELECT ts,code,name,strategy,price,change_pct,reason FROM signals "
        params: list = []
        if code:
            sql += "WHERE code=? "
            params.append(code)
        sql += "ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return [{"ts": r[0], "code": r[1], "name": r[2], "strategy": r[3],
                 "price": r[4], "change_pct": r[5], "reason": r[6]} for r in rows]

    def last_signal_time(self, code: str, strategy: str) -> Optional[float]:
        """最近一次信号的 epoch 时间（用于去重）"""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT ts FROM signals WHERE code=? AND strategy=? "
                    "ORDER BY id DESC LIMIT 1", (code, strategy)).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        try:
            return time.mktime(time.strptime(row[0], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None
