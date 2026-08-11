# -*- coding: utf-8 -*-
"""回测数据加载：本地缓存优先 + 缺失区间增量拉取合并

策略：
- 缓存不按天失效（长期复用，避免全量重拉触发数据源限速）
- 已有缓存时，只拉"缓存覆盖不到的区间"：
  · 尾部缺口（最常见：昨日缓存补今日新数据）→ 拉 [缓存末根次日, end]
  · 头部缺口（用户提前 start）→ 拉 [warmup_start, 缓存首根前日]
  · 两段都缺则分别补
- 增量数据与缓存按 ts 去重合并后整段落库（主键 upsert）
- 增量拉取失败时退回旧缓存（尽力而为，缺口由覆盖警告提示）
"""
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# 各周期每个交易日的K线根数（估算增量期望根数用）
PERIOD_BARS_PER_DAY = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4,
                       "daily": 1, "weekly": 0.2}
# 数据源单次拉取根数上限（约两年半：避免长历史拉取触发数据源限速）
PERIOD_FETCH_CAP = {"1m": 8000, "5m": 8000, "15m": 8000, "30m": 5000, "60m": 2500,
                    "daily": 700, "weekly": 200}
# 串行拉取节流间隔（秒）：BaoStock 等源对连续请求限速，逐只拉取并间隔
FETCH_THROTTLE_SECONDS = 0.5
# 覆盖容差（自然日）：请求起点/终点是自然日，数据从相邻交易日开始，避免节假日错位误判
_COVER_TOLERANCE_DAYS = 7


def day_gap(a: str, b: str) -> int:
    """两个日期字符串（YYYY-MM-DD 或含时分）的自然日差，解析失败返回 0"""
    try:
        return (datetime.strptime(a[:10], "%Y-%m-%d")
                - datetime.strptime(b[:10], "%Y-%m-%d")).days
    except ValueError:
        return 0


def _expect_bars(period: str, start: str, end: str) -> int:
    """估算区间期望K线根数（带 30% 余量 + 预热，受单次拉取上限约束）"""
    bpd = PERIOD_BARS_PER_DAY.get(period, 8)
    try:
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
        days = max((e - s).days, 1)
    except ValueError:
        days = 10
    need = int(days * bpd * 1.3) + 10
    return min(max(need, 30), PERIOD_FETCH_CAP.get(period, 5000))


def _merge_bars(*lists: List[dict]) -> List[dict]:
    """多段K线按 ts 去重合并（后者覆盖前者同 ts），升序返回"""
    by_ts: Dict[str, dict] = {}
    for lst in lists:
        for k in lst:
            by_ts[k["ts"]] = k
    return [by_ts[k] for k in sorted(by_ts)]


async def load_kline_merged(ds, cache, code: str, period: str, need: int,
                            warmup_start: str = "", end: str = ""
                            ) -> Tuple[List[dict], bool]:
    """增量加载单只股票K线并落缓存。

    返回 (klines升序, from_cache)：
    - from_cache=True：纯缓存命中（无在线拉取）或增量拉取失败退回缓存
    - from_cache=False：发生了在线拉取（全量或增量）
    """
    cached = cache.get_all(code, period)
    if not cached:
        # 无缓存：全量拉取
        kl = await ds.kline(code, period, need, min_len=need,
                            start_date=warmup_start, end_date=end)
        if kl:
            cache.put(code, period, kl)
        return kl or [], False

    # 已有缓存：判断缺口（带交易日容差）
    head_gap = bool(warmup_start) and day_gap(cached[0]["ts"], warmup_start) > _COVER_TOLERANCE_DAYS
    tail_gap = bool(end) and day_gap(end, cached[-1]["ts"]) > _COVER_TOLERANCE_DAYS
    if not head_gap and not tail_gap:
        return cached, True   # 完全覆盖，纯缓存

    merged = list(cached)
    updated = False
    if head_gap:
        head_end = (datetime.strptime(cached[0]["ts"][:10], "%Y-%m-%d")
                    - timedelta(days=1)).strftime("%Y-%m-%d")
        expect = _expect_bars(period, warmup_start, head_end)
        kl = await ds.kline(code, period, expect, min_len=expect,
                            start_date=warmup_start, end_date=head_end)
        if kl:
            merged = _merge_bars(kl, merged)
            updated = True
    if tail_gap:
        tail_start = (datetime.strptime(cached[-1]["ts"][:10], "%Y-%m-%d")
                      + timedelta(days=1)).strftime("%Y-%m-%d")
        expect = _expect_bars(period, tail_start, end)
        kl = await ds.kline(code, period, expect, min_len=expect,
                            start_date=tail_start, end_date=end)
        if kl:
            merged = _merge_bars(merged, kl)
            updated = True
    if updated and len(merged) > len(cached):
        cache.put(code, period, merged)
        return merged, False
    return cached, True   # 增量无新数据/拉取失败，退回旧缓存
