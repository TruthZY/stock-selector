# -*- coding: utf-8 -*-
"""交易日历：判断今天是否为 A 股交易日（周末 + 节假日）。

现有 app.scanner.is_trading_time 只判周一~周五、不含节假日。这里独立加"快照哨兵"：
腾讯快照每只股票带 time 字段（YYYYMMDDHHMMSS）。**非交易日（节假日）时，快照返回
的是上一交易日的数据、time 的日期 < 今日**——据此可靠识别节假日，避免用陈旧快照
合成"假的当日日K"而产生错误信号。

探测多只流动性好的股票，任一 time 日期 == 今日 即判为交易日（防个别停牌误判）。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import List, Tuple

# 探测用的股票数（从股票池取前若干只大盘股）
PROBE_COUNT = 10


def today8() -> str:
    return time.strftime("%Y%m%d")


def is_weekend(d: datetime | None = None) -> bool:
    d = d or datetime.now()
    return d.weekday() >= 5      # 5=周六 6=周日


def _snap_date(snap: dict) -> str:
    """从快照 time 字段(YYYYMMDDHHMMSS)取日期 8 位；无则空串。"""
    t = str(snap.get("time", "") or "")
    digits = "".join(ch for ch in t if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


async def classify_trading_day(ds, probe_codes: List[str],
                               today: str | None = None) -> Tuple[str, str]:
    """判定今日交易状态。

    返回 (status, reason)，status ∈ {"trading","closed","unknown"}：
      trading : 确认为交易日（探针快照时间日==今日）
      closed  : 确认非交易日（周末，或探针快照全为上一交易日 → 节假日）
      unknown : 无法判定（快照拉取失败/为空）——调用方应让扫描继续，
                由覆盖率兜底处理，而不是当作交易日强行合成当日bar
    """
    today = today or time.strftime("%Y-%m-%d")
    t8 = today.replace("-", "")

    if is_weekend(datetime.strptime(today, "%Y-%m-%d")):
        return "closed", "周末"

    codes = list(probe_codes)[:PROBE_COUNT]
    if not codes:
        return "unknown", "无探针股票"
    try:
        snaps = await ds.snapshots(codes)
    except Exception as e:
        return "unknown", f"快照异常:{type(e).__name__}"
    if not snaps:
        return "unknown", "快照为空(网络?)"

    dates = {_snap_date(s) for s in snaps.values() if _snap_date(s)}
    if t8 in dates:
        return "trading", f"探针快照时间日={t8}"
    latest = max(dates) if dates else ""
    return "closed", f"非交易日(节假日?)，探针最新时间日={latest}"
