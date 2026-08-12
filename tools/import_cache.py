# -*- coding: utf-8 -*-
"""导入回测K线缓存快照：python tools/import_cache.py <kline_cache_YYYYMMDD.db.gz>

按主键 (code, period, ts) upsert 合并进本机 kline_cache，不是覆盖式替换：
本机已有的数据保留，快照里更新的同 ts 行取快照值，新 ts 行插入。
所以本机跑过 download.py 之后再导入快照也安全。

只写 kline_cache，不碰实时系统的 kline_daily/kline_min，
也不动 stocks/watch/signals 等本机状态。

导入后会做一次 OHLC 越界体检（high<low 之类），防止导入被污染的快照。
"""
import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from app.backtest.cache import KlineCache

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _stat(conn) -> tuple:
    return conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT code), MIN(ts), MAX(ts) FROM kline_cache").fetchone()


def import_snapshot(snapshot: str, db_path: str) -> dict:
    tmp = snapshot + ".tmp.db"
    if os.path.exists(tmp):
        os.remove(tmp)
    opener = gzip.open if snapshot.endswith(".gz") else open
    with opener(snapshot, "rb") as f, open(tmp, "wb") as g:
        shutil.copyfileobj(f, g, length=1 << 20)

    try:
        probe = sqlite3.connect(tmp)
        try:
            src_stat = _stat(probe)
            bad = probe.execute(
                "SELECT COUNT(*) FROM kline_cache WHERE high<low OR high<open "
                "OR high<close OR low>open OR low>close").fetchone()[0]
        finally:
            probe.close()
        if not src_stat[0]:
            raise ValueError("快照里的 kline_cache 是空表")

        # 确保本机库与表存在（KlineCache 构造时会建表）
        KlineCache(db_path)
        dst = sqlite3.connect(db_path, timeout=30)
        try:
            before = _stat(dst)
            dst.execute("ATTACH ? AS s", (tmp,))
            dst.execute("INSERT OR REPLACE INTO main.kline_cache "
                        "SELECT * FROM s.kline_cache")
            dst.commit()
            dst.execute("DETACH s")
            after = _stat(dst)
        finally:
            dst.close()
    finally:
        os.remove(tmp)

    return {"src": src_stat, "before": before, "after": after, "bad": bad}


def main():
    parser = argparse.ArgumentParser(description="导入回测K线缓存快照（upsert 合并）")
    parser.add_argument("snapshot", help="快照文件 .db.gz 或 .db")
    args = parser.parse_args()

    if not os.path.exists(args.snapshot):
        print(f"找不到文件 {args.snapshot}")
        sys.exit(1)

    print(f"导入 {args.snapshot} -> {config.DB_PATH}")
    t0 = time.time()
    try:
        r = import_snapshot(args.snapshot, config.DB_PATH)
    except Exception as e:
        print(f"导入失败: {type(e).__name__}: {e}")
        sys.exit(1)

    src, before, after = r["src"], r["before"], r["after"]
    print(f"  快照: {src[0]:,} 行 | {src[1]} 只 | {src[2]} ~ {src[3]}")
    print(f"  本机: {before[0]:,} 行 -> {after[0]:,} 行（新增 {after[0] - before[0]:,}）")
    print(f"        股票 {before[1]} -> {after[1]} 只 | 区间 {after[2]} ~ {after[3]}")
    if r["bad"]:
        print(f"  [警告] 快照中有 {r['bad']:,} 行 OHLC 越界（high<low 等），"
              f"疑似在 BaoStock high/low 修复前生成，建议改用 download.py --force 重下")
    else:
        print("  OHLC 体检: 通过")
    print(f"  耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
