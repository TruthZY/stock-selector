# -*- coding: utf-8 -*-
"""导出回测K线缓存为可分发的压缩快照：python tools/export_cache.py [-o 文件名]

只导出 kline_cache 一张表（回测数据），不含实时系统的 kline_daily/kline_min，
也不含 stocks/watch/signals 等本机状态 —— 快照是纯行情数据，换机器直接导入即用。

一致性：在源库上开一个读事务后再整表复制，WAL 模式下拿到的是事务开始时刻的
一致快照，因此选股服务正在运行、-wal 里有未落盘数据时也能安全导出。

配套导入：python tools/import_cache.py <文件>
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
from app.backtest.cache import _SCHEMA

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _mb(path: str) -> float:
    return os.path.getsize(path) / 1024 / 1024


def export(db_path: str, out_path: str, level: int = 9) -> dict:
    tmp = out_path + ".tmp.db"
    for p in (tmp, out_path):
        if os.path.exists(p):
            os.remove(p)

    # 先用独立连接按正式 schema 建好目标表（带主键与索引），保证与生产库同构
    dst = sqlite3.connect(tmp)
    dst.executescript(_SCHEMA)
    dst.commit()
    dst.close()

    src = sqlite3.connect(db_path, timeout=30)
    try:
        # 显式读事务：WAL 下固定快照点，避免复制期间被并发写入撕裂
        src.execute("BEGIN")
        stat = src.execute(
            "SELECT COUNT(*), COUNT(DISTINCT code), MIN(ts), MAX(ts) "
            "FROM kline_cache").fetchone()
        periods = [r[0] for r in src.execute(
            "SELECT DISTINCT period FROM kline_cache ORDER BY period")]
        src.execute("ATTACH ? AS d", (tmp,))
        src.execute("INSERT OR REPLACE INTO d.kline_cache SELECT * FROM main.kline_cache")
        src.commit()
        src.execute("DETACH d")
    finally:
        src.close()

    raw = _mb(tmp)
    with open(tmp, "rb") as f, gzip.open(out_path, "wb", compresslevel=level) as g:
        shutil.copyfileobj(f, g, length=1 << 20)
    os.remove(tmp)

    return {"rows": stat[0], "codes": stat[1], "first_ts": stat[2], "last_ts": stat[3],
            "periods": periods, "raw_mb": raw, "gz_mb": _mb(out_path)}


def main():
    parser = argparse.ArgumentParser(description="导出回测K线缓存快照（gzip 压缩）")
    parser.add_argument("-o", "--out", help="输出文件名（默认 kline_cache_YYYYMMDD.db.gz）")
    parser.add_argument("--level", type=int, default=9, help="gzip 压缩级别 1-9（默认 9）")
    args = parser.parse_args()

    out = args.out or f"kline_cache_{time.strftime('%Y%m%d')}.db.gz"
    if not os.path.exists(config.DB_PATH):
        print(f"找不到数据库 {config.DB_PATH}")
        sys.exit(1)

    print(f"导出 {config.DB_PATH} 的 kline_cache -> {out}")
    t0 = time.time()
    info = export(config.DB_PATH, out, args.level)
    if not info["rows"]:
        print("kline_cache 为空，先跑 download.py 再导出")
        os.remove(out)
        sys.exit(1)

    print(f"  行数 {info['rows']:,} | 股票 {info['codes']} 只 | "
          f"周期 {', '.join(info['periods'])}")
    print(f"  区间 {info['first_ts']} ~ {info['last_ts']}")
    print(f"  体积 {info['raw_mb']:.1f} MB -> 压缩后 {info['gz_mb']:.1f} MB "
          f"({info['gz_mb'] / info['raw_mb'] * 100:.0f}%)")
    print(f"  耗时 {time.time() - t0:.0f}s")
    if info["gz_mb"] > 2048:
        print("  [警告] 超过 GitHub Release 单文件 2 GB 上限")
    print(f"\n上传为 Release 附件：")
    print(f"  gh release create data-{time.strftime('%Y%m%d')} {out} "
          f"-t \"回测数据快照 {time.strftime('%Y-%m-%d')}\" -n \"kline_cache 快照\"")
    print(f"其他机器导入：\n  python tools/import_cache.py {out}")


if __name__ == "__main__":
    main()
