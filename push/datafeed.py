# -*- coding: utf-8 -*-
"""数据装配（复用现有数据源/缓存，不改动它们）

职责：
  1. 批量实时快照（腾讯，按 50 只分批，与实盘 Scanner 同节奏）；
  2. 日线历史增量补齐（优先读 kline_cache，陈旧才在线补，且**只补今日之前**的
     收盘bar，绝不把盘中/当日bar写进 kline_cache，避免污染回测数据）；
  3. 用实时快照合成"当日日K"并拼到历史末尾（仅内存视图，复用
     Scanner._live_daily_bar 的逻辑）。

对既有代码只做只读 import：DataSource / KlineCache / loader.day_gap /
Scanner._live_daily_bar。
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

# 腾讯批量快照单批上限（与实盘 config.SNAPSHOT_BATCH_SIZE 一致）
SNAPSHOT_BATCH = 50
# 历史补齐时单只拉取根数（够 accumulation 的 200 日窗口 + 余量）
HIST_NEED = 300
# 历史"陈旧"判定：末根收盘bar距今超过该自然日数才触发在线补齐
STALE_DAYS = 4
# 作业B 盘后刷新时单只拉取的近端根数（含当日收盘bar，合并入缓存即可）
REFRESH_LIMIT = 15


def today_str() -> str:
    return time.strftime("%Y-%m-%d")


def _day_gap(a: str, b: str) -> int:
    """复用 loader 的自然日差；导入失败时本地兜底。"""
    try:
        from app.backtest.loader import day_gap
        return day_gap(a, b)
    except Exception:
        from datetime import datetime
        try:
            return (datetime.strptime(a[:10], "%Y-%m-%d")
                    - datetime.strptime(b[:10], "%Y-%m-%d")).days
        except ValueError:
            return 0


# ---------------------------------------------------------------------------
# 当日日K合成：优先复用实盘 Scanner 的静态方法，导入失败则用等价本地实现
# ---------------------------------------------------------------------------
def _live_daily_bar_fallback(snap: dict) -> Optional[dict]:
    try:
        price = float(snap.get("price") or 0)
        if price <= 0:
            return None
        return {"ts": today_str(),
                "open": float(snap.get("open") or price),
                "high": float(snap.get("high") or price),
                "low": float(snap.get("low") or price),
                "close": price,
                "volume": float(snap.get("volume") or 0),
                "amount": float(snap.get("amount") or 0)}
    except (TypeError, ValueError):
        return None


def synthesize_live_daily_bar(snap: Optional[dict]) -> Optional[dict]:
    if not snap:
        return None
    try:
        from app.scanner import Scanner
        bar = Scanner._live_daily_bar(snap)
        return bar if bar is not None else _live_daily_bar_fallback(snap)
    except Exception:
        return _live_daily_bar_fallback(snap)


def build_bars(hist: Optional[List[dict]], snap: Optional[dict],
               period: str = "daily") -> List[dict]:
    """拼出喂给规则的完整序列。

    daily：历史严格取「今日之前」的收盘bar（剔除缓存里可能残留的当日bar），
           再追加实时快照合成的当日bar——今日那根只来自快照，只存在内存里。
    其它周期（30m 等，预留）：直接用历史（收盘bar由作业B/数据源保证），不合成。
    """
    hist = hist or []
    if period != "daily":
        return list(hist)
    today = today_str()
    bars = [b for b in hist if b.get("ts", "")[:10] < today]
    live = synthesize_live_daily_bar(snap)
    if live is not None:
        bars.append(live)
    return bars


def build_bars_close(hist: Optional[List[dict]], period: str = "daily",
                     today: Optional[str] = None) -> List[dict]:
    """盘后装配：直接用缓存历史（作业B已把今日**收盘**bar落库），截断到 <= today。

    与 build_bars 的区别：不拉实时快照、不合成当日bar——今日那根就是缓存里的官方
    收盘bar。用于 21:00 盘后扫描（"只用盘后数据不用实时数据"）。
    """
    hist = hist or []
    today = today or today_str()
    return [b for b in hist if b.get("ts", "")[:10] <= today]


# ---------------------------------------------------------------------------
# 批量快照 & 历史加载（都带 deadline，超时即止）
# ---------------------------------------------------------------------------
async def fetch_snapshots(ds, codes: List[str], deadline: Optional[float] = None,
                          batch: int = SNAPSHOT_BATCH) -> Dict[str, dict]:
    """分批拉实时快照，合并返回 {code: snap}。到 deadline 停止后续批次。"""
    out: Dict[str, dict] = {}
    for i in range(0, len(codes), batch):
        if deadline is not None and time.time() > deadline:
            break
        chunk = codes[i:i + batch]
        try:
            out.update(await ds.snapshots(chunk))
        except Exception:
            # 单批失败不阻断整体，缺的股票后续按"数据不齐"跳过
            continue
    return out


def _is_stale(hist: Optional[List[dict]], today: str, stale_days: int = STALE_DAYS) -> bool:
    if not hist:
        return True
    # 末根可能是今日（收盘后缓存已含今日）；用 max(今日, 末根) 判断落后程度
    return _day_gap(today, hist[-1].get("ts", "")) > stale_days


async def load_histories(ds, cache, codes: List[str], period: str,
                         deadline: Optional[float] = None,
                         concurrency: int = 10, need: int = HIST_NEED,
                         today: Optional[str] = None) -> Dict[str, List[dict]]:
    """加载各股历史K线（优先缓存，陈旧才在线补，且只补今日之前的收盘bar）。

    返回 {code: bars升序}；无数据的股票不在结果里。到 deadline 停止发起新请求。
    """
    today = today or today_str()
    sem = asyncio.Semaphore(concurrency)
    out: Dict[str, List[dict]] = {}

    async def one(code: str):
        if deadline is not None and time.time() > deadline:
            return
        async with sem:
            hist = None
            try:
                hist = cache.get_all(code, period)
            except Exception:
                hist = None
            if _is_stale(hist, today):
                # 安全网：在线补历史，但严格剔除今日bar后再落缓存（不污染回测数据）
                try:
                    kl = await ds.kline(code, period, need)
                    hist_only = [b for b in (kl or []) if b.get("ts", "")[:10] < today]
                    if hist_only:
                        cache.put(code, period, hist_only)
                        hist = hist_only
                except Exception:
                    pass
            if hist:
                out[code] = hist

    await asyncio.gather(*(one(c) for c in codes))
    return out


async def refresh_to_today(ds, cache, codes: List[str], period: str, today: str,
                           deadline: Optional[float] = None, concurrency: int = 10,
                           fetch_limit: int = REFRESH_LIMIT):
    """作业B：盘后把当日**已收盘**K线增量补进 kline_cache（含今日那根）。

    为什么不用 load_kline_merged：它带 7 天覆盖容差，缓存只差"今日一根"时会被判为
    已覆盖而不拉取。这里直接小批量拉近端 fetch_limit 根（数据源收盘后已含今日bar），
    用 cache.put(INSERT OR REPLACE) 合并入缓存——只覆盖近端若干根，不动更早历史。

    返回 (fetched, landed)：
      fetched = 成功在线拉取并写入的股票；
      landed  = 复核缓存末根日期 == today 的股票（落库校验，供上层判断是否重试）。
    """
    sem = asyncio.Semaphore(concurrency)
    fetched: List[str] = []

    async def one(code: str):
        if deadline is not None and time.time() > deadline:
            return
        async with sem:
            try:
                kl = await ds.kline(code, period, fetch_limit)
                if kl:
                    cache.put(code, period, kl)
                    fetched.append(code)
            except Exception:
                pass

    await asyncio.gather(*(one(c) for c in codes))

    landed: List[str] = []
    for code in codes:
        try:
            hist = cache.get_all(code, period)
        except Exception:
            hist = None
        if hist and hist[-1].get("ts", "")[:10] == today:
            landed.append(code)
    return fetched, landed
