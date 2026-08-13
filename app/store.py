# -*- coding: utf-8 -*-
"""SQLite 存储层：股票池、K线、信号

K线表 klines 按 (code, period, ts) 隔离每个周期。历史上曾用
kline_daily + kline_min 两张表且无周期列，导致 1m 与 60m 的 ts 撞主键、
互相顶掉——见 _migrate
"""
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

import config

# schema 版本（PRAGMA user_version）：v1 = klines 取代 kline_daily/kline_min
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code    TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    market  TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS klines (
    code   TEXT NOT NULL,
    period TEXT NOT NULL,
    ts     TEXT NOT NULL,
    open   REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (code, period, ts)
);
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
CREATE TABLE IF NOT EXISTS custom_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS custom_group_stocks (
    group_id INTEGER NOT NULL,
    code     TEXT NOT NULL,
    name     TEXT NOT NULL,
    added_at TEXT DEFAULT '',
    PRIMARY KEY (group_id, code)
);
"""


class Store:
    """SQLite 存储（线程安全：每个连接独立 + 写锁）"""

    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._migrate(conn)
            self._migrate_custom(conn)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------- 迁移 -------------------------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把旧的 kline_daily/kline_min 迁进带周期的 klines 表（schema v1）

        必须可重入且跨进程安全：Store() 在服务、下载器、回测引擎、验证器
        四个入口各自构造，可能是并发的独立进程
        """
        if conn.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
            return                      # 快路径：已迁移，不取锁
        # Py3.11 legacy 模式下 DDL 走 autocommit，不置 None 则手动 BEGIN 会报
        # "cannot start a transaction within a transaction"。仅作用于本连接
        conn.isolation_level = None
        # BEGIN IMMEDIATE 立刻取写锁：busy_timeout 救不了「读→写」锁升级，
        # 那种情况会直接返回 SQLITE_BUSY
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 取到写锁后重查版本：否则竞争失败的进程会在 A 提交后继续执行，
            # 撞 "no such table: kline_daily"——而这发生在 __init__ 里，服务直接起不来
            if conn.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
                conn.execute("ROLLBACK")
                return
            has = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            moved = {}
            if "kline_daily" in has:
                # 日线无损全量搬迁
                cur = conn.execute(
                    "INSERT OR IGNORE INTO klines"
                    "(code,period,ts,open,high,low,close,volume,amount) "
                    "SELECT code,'daily',ts,open,high,low,close,volume,amount "
                    "FROM kline_daily")
                moved["daily"] = cur.rowcount
                conn.execute("DROP TABLE kline_daily")
            if "kline_min" in has:
                # kline_min 混存 1m/30m/60m 且无周期列，1m 的 10:30 与 60m 的 10:30
                # 逐字节相同，只能按「按天密度」判别：干净日每股 ≤8 根，
                # 有分钟洪水的日子每股约 240 根。
                # 必须按天而非按 (股票,天) 判别——个别只被看过图的代码当天恰好只有
                # 8 根且全是 30m 槽位，按股票判别会误标成 60m
                cur = conn.execute(
                    "INSERT OR IGNORE INTO klines"
                    "(code,period,ts,open,high,low,close,volume,amount) "
                    "SELECT code,'60m',ts,open,high,low,close,volume,amount "
                    "FROM kline_min "
                    "WHERE substr(ts,12,5) IN ('10:30','11:30','14:00','15:00') "
                    "  AND substr(ts,1,10) IN ("
                    "        SELECT substr(ts,1,10) FROM kline_min "
                    "        GROUP BY substr(ts,1,10) "
                    "        HAVING COUNT(*) <= 8 * ("
                    "            SELECT COUNT(DISTINCT code) FROM kline_min))")
                moved["60m"] = cur.rowcount
                # 余下的 1m 洪水与带洞的 30m（撞车只存下 4/8 根）一并丢弃，
                # 不可用且可由 _init_history 重新拉取
                conn.execute("DROP TABLE kline_min")
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if moved:
            print("[store] K线表迁移完成: " + ", ".join(
                f"{p} {n} 行" for p, n in moved.items()), flush=True)

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
                conn.execute("DELETE FROM klines WHERE code=?", (code,))
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

    # ------------------------- 自定义分组 -------------------------
    # 用户自建的股票分组（可增删改名），供战法筛选面板作为扫描范围。
    # 组内股票参与 Scanner 的行情轮询（见 _monitored_codes），但不评估策略信号

    def _migrate_custom(self, conn: sqlite3.Connection) -> None:
        """把早期单表 custom 的数据迁进分组结构（若存在旧表且有数据）"""
        has = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "custom" not in has:
            return
        rows = conn.execute(
            "SELECT code,name,added_at FROM custom ORDER BY added_at").fetchall()
        if rows:
            cur = conn.execute(
                "INSERT INTO custom_groups(name,created_at) VALUES(?,?)",
                ("自定义", time.strftime("%Y-%m-%d %H:%M:%S")))
            conn.executemany(
                "INSERT OR IGNORE INTO custom_group_stocks"
                "(group_id,code,name,added_at) VALUES(?,?,?,?)",
                [(cur.lastrowid, r[0], r[1], r[2] or "") for r in rows])
        conn.execute("DROP TABLE custom")
        conn.commit()

    def get_groups(self) -> List[dict]:
        """全部分组及组内股票：[{id, name, stocks:[{code,name}]}]（按创建顺序）"""
        with self._lock:
            conn = self._connect()
            try:
                groups = [{"id": r[0], "name": r[1], "stocks": []}
                          for r in conn.execute(
                              "SELECT id,name FROM custom_groups ORDER BY id")]
                rows = conn.execute(
                    "SELECT group_id,code,name FROM custom_group_stocks "
                    "ORDER BY added_at, code").fetchall()
            finally:
                conn.close()
        for gid, code, name in rows:
            g = next((x for x in groups if x["id"] == gid), None)
            if g:
                g["stocks"].append({"code": code, "name": name})
        return groups

    def add_group(self, name: str) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                try:
                    cur = conn.execute(
                        "INSERT INTO custom_groups(name,created_at) VALUES(?,?)",
                        (name, time.strftime("%Y-%m-%d %H:%M:%S")))
                    conn.commit()
                except sqlite3.IntegrityError:
                    return {"ok": False, "msg": f"组「{name}」已存在"}
                return {"ok": True, "msg": f"已新建组：{name}", "id": cur.lastrowid}
            finally:
                conn.close()

    def rename_group(self, gid: int, name: str) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                try:
                    cur = conn.execute(
                        "UPDATE custom_groups SET name=? WHERE id=?", (name, gid))
                    conn.commit()
                except sqlite3.IntegrityError:
                    return {"ok": False, "msg": f"组「{name}」已存在"}
                if cur.rowcount == 0:
                    return {"ok": False, "msg": "组不存在（可能已被删除）"}
            finally:
                conn.close()
        return {"ok": True, "msg": f"已改名为：{name}"}

    def remove_group(self, gid: int) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM custom_group_stocks WHERE group_id=?", (gid,))
                conn.execute("DELETE FROM custom_groups WHERE id=?", (gid,))
                conn.commit()
            finally:
                conn.close()
        return {"ok": True, "msg": "组已删除"}

    def add_group_stock(self, gid: int, code: str, name: str) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT name FROM custom_groups WHERE id=?",
                                   (gid,)).fetchone()
                if not row:
                    return {"ok": False, "msg": "组不存在（可能已被删除）"}
                conn.execute(
                    "INSERT OR REPLACE INTO custom_group_stocks"
                    "(group_id,code,name,added_at) VALUES(?,?,?,?)",
                    (gid, code, name, time.strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            finally:
                conn.close()
        return {"ok": True, "msg": f"已加入 {name} {code}"}

    def remove_group_stock(self, gid: int, code: str) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM custom_group_stocks WHERE group_id=? AND code=?",
                    (gid, code))
                conn.commit()
            finally:
                conn.close()
        return {"ok": True, "msg": f"已移出 {code}"}

    def get_group_stocks(self, gid: int) -> List[Tuple[str, str]]:
        with self._lock:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT code,name FROM custom_group_stocks WHERE group_id=? "
                    "ORDER BY added_at, code", (gid,)).fetchall()
            finally:
                conn.close()

    def get_all_group_stocks(self) -> List[Tuple[str, str]]:
        """全部组内股票（去重），供行情轮询"""
        with self._lock:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT DISTINCT code,name FROM custom_group_stocks").fetchall()
            finally:
                conn.close()

    # ------------------------- K线 -------------------------

    def upsert_klines(self, code: str, klines: List[dict], period: str) -> int:
        """写入某周期K线，返回处理条数

        缺失的K线正常插入；已存在的K线**只允许改写最新两根**——盘中拉到的
        当前K线是未收盘的残缺值，收盘后必须被纠正，否则永久错误。
        但不能无条件覆盖：数据源降级链的复权基准并不一致（腾讯分钟线与新浪
        都不复权，东财/BaoStock 前复权），无条件覆盖会让熔断期间写入的一批K线
        在基准之间来回翻转，而序列尾部的整体跳变恰是 cross_up 这类只看最后
        两点的判断最怕的——一次翻转就是一次假金叉。
        限定最新两根即可：同步间隔 30s 远小于最小周期 30min，一根K线一旦
        有了更新的邻居，就不可能还是残缺的
        """
        if not klines:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT MIN(ts) FROM (SELECT ts FROM klines "
                    "WHERE code=? AND period=? ORDER BY ts DESC LIMIT 2)",
                    (code, period)).fetchone()
                cutoff = (row[0] if row else None) or ""
                conn.executemany(
                    "INSERT INTO klines"
                    "(code,period,ts,open,high,low,close,volume,amount) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(code,period,ts) DO UPDATE SET "
                    "  open=excluded.open, high=excluded.high, low=excluded.low, "
                    "  close=excluded.close, volume=excluded.volume, "
                    "  amount=excluded.amount "
                    "WHERE excluded.ts >= ?",       # 谓词不满足是 no-op，不报错
                    [(code, period, k["ts"], k["open"], k["high"], k["low"],
                      k["close"], k["volume"], k["amount"], cutoff) for k in klines])
                conn.commit()
                return len(klines)
            finally:
                conn.close()

    def get_klines(self, code: str, period: str, limit: int = 300) -> List[dict]:
        """取某周期最近 limit 根K线（升序）"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT ts,open,high,low,close,volume,amount FROM klines "
                    "WHERE code=? AND period=? ORDER BY ts DESC LIMIT ?",
                    (code, period, limit)).fetchall()
            finally:
                conn.close()
        out = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                "close": r[4], "volume": r[5], "amount": r[6]} for r in rows]
        out.reverse()
        return out

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
