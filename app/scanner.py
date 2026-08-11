# -*- coding: utf-8 -*-
"""实时扫描器：快照轮询 + K线增量同步 + 选股信号触发 + 事件广播"""
import asyncio
import time
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional

import config
from app import indicators as ta
from app import strategies as strat
from app.datasource import DataSource
from app.store import Store

EventCallback = Callable[[dict], Awaitable[None]]


def is_trading_time(now: Optional[datetime] = None) -> bool:
    """A股交易时段（周一至周五 9:00-15:30，忽略节假日）"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 <= hm <= 15 * 60 + 30


class Scanner:
    """后台扫描任务"""

    def __init__(self, store: Store, ds: DataSource, engine: strat.StrategyEngine,
                 emit: EventCallback):
        self.store = store
        self.ds = ds
        self.engine = engine
        self.emit = emit                      # 事件广播（WebSocket）
        self.snapshots: Dict[str, dict] = {}  # 最新快照缓存
        self.daily_cache: Dict[str, List[dict]] = {}
        self.k60_cache: Dict[str, List[dict]] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self.started_at = time.time()
        self.last_snapshot_at = 0.0
        self.last_kline_sync_at = 0.0
        self.snapshot_ok = 0
        self.snapshot_fail = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self):
        self._running = True
        await self._init_pool()
        await self._init_history()
        self._tasks = [
            asyncio.create_task(self._snapshot_loop()),
            asyncio.create_task(self._kline_loop()),
        ]

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        await self.ds.close()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def _init_pool(self):
        """初始化股票池：用户配置 > 沪深300(可选) > 内置池"""
        pool = config.load_user_pool()
        if not pool and config.ENABLE_HS300_POOL:
            try:
                hs = await self.ds.hs300()
                pool = hs or None
            except Exception:
                pool = None
        if not pool:
            pool = config.BUILTIN_POOL
        self.store.upsert_stocks(pool)
        await self._broadcast({
            "type": "status",
            "data": {"msg": f"股票池已加载 {len(pool)} 只股票"},
        })

    async def _init_history(self):
        """首次启动拉取历史K线（日K + 60分钟K），失败的股票逐个重试补齐"""
        stocks = self.store.get_stocks()
        codes = [c for c, _ in stocks]
        daily = await self.ds.fetch_many_kline(
            codes, "daily", config.DAILY_HISTORY_LIMIT, config.KLINE_CONCURRENCY)
        k60 = await self.ds.fetch_many_kline(
            codes, "60m", config.MINUTE_HISTORY_LIMIT, config.KLINE_CONCURRENCY)
        # 补齐拉取失败的股票（单个串行重试，降低被限流概率）
        for code in codes:
            if code not in daily:
                data = await self.ds.kline(code, "daily", config.DAILY_HISTORY_LIMIT)
                if data:
                    daily[code] = data
            if code not in k60:
                data = await self.ds.kline(code, "60m", config.MINUTE_HISTORY_LIMIT)
                if data:
                    k60[code] = data
        for code, kl in daily.items():
            self.store.upsert_klines(code, kl, "kline_daily")
        for code, kl in k60.items():
            self.store.upsert_klines(code, kl, "kline_min")
        # 重建缓存
        for code, _ in stocks:
            self.daily_cache[code] = self.store.get_klines(code, "kline_daily", 400)
            self.k60_cache[code] = self.store.get_klines(code, "kline_min", 400)
        await self._broadcast({
            "type": "status",
            "data": {"msg": f"历史K线加载完成：日K {len(daily)} 只 / 60分钟K {len(k60)} 只"},
        })

    # ------------------------------------------------------------------
    # 监控范围
    # ------------------------------------------------------------------

    def _monitored_codes(self) -> List[str]:
        """监控目标 = 股票池 + 自选股（自选股只参与行情轮询，不评估策略）"""
        codes = [c for c, _ in self.store.get_stocks()]
        pool = set(codes)
        codes += [c for c, _ in self.store.get_watch() if c not in pool]
        return codes

    # ------------------------------------------------------------------
    # 快照轮询
    # ------------------------------------------------------------------

    async def _snapshot_loop(self):
        while self._running:
            cycle_start = time.time()
            trading = is_trading_time()
            interval = config.SNAPSHOT_INTERVAL if trading else config.OFFLINE_SNAPSHOT_INTERVAL
            codes = self._monitored_codes()
            # 分批拉取，批间均分间隔，避免瞬时打满
            batch_size = config.SNAPSHOT_BATCH_SIZE
            ok, fail = 0, 0
            for i in range(0, len(codes), batch_size):
                batch = codes[i:i + batch_size]
                data = await self.ds.snapshots(batch)
                ok += len(data)
                fail += len(batch) - len(data)
                for code, snap in data.items():
                    snap["_ts"] = time.time()
                    self.snapshots[code] = snap
                if len(codes) > batch_size:
                    await asyncio.sleep(interval * batch_size / len(codes))
            self.last_snapshot_at = time.time()
            self.snapshot_ok, self.snapshot_fail = ok, fail
            self._attach_volume_ratio()
            # 实时类策略扫描（休市时行情不变，跳过避免信号重复刷）
            hits = await self._scan_snapshot_strategies() if trading else []
            await self._broadcast({
                "type": "snapshots",
                "data": {"snapshots": list(self.snapshots.values()), "hits": hits,
                         "trading": trading, "ts": time.time()},
            })
            await self._broadcast_status(f"快照更新 {ok} 只" + (f"（失败 {fail}）" if fail else ""))
            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(0.2, interval - elapsed))

    def _attach_volume_ratio(self):
        """为快照附加量比（当日成交量/前5日均量），供前端展示；缓存缺失时兜底读库"""
        for code, snap in self.snapshots.items():
            daily = self.daily_cache.get(code, [])
            if len(daily) <= 5:
                daily = self.store.get_klines(code, "kline_daily", 400)
                if daily:
                    self.daily_cache[code] = daily
            if daily and len(daily) > 5:
                snap["volume_ratio"] = ta.volume_ratio(
                    snap["volume"], [k["volume"] for k in daily[:-1]])

    async def _scan_snapshot_strategies(self) -> List[dict]:
        """基于最新快照扫描实时类策略，命中即入库+广播（仅股票池参与）"""
        hits: List[dict] = []
        pool_codes = {c for c, _ in self.store.get_stocks()}
        for code, snap in self.snapshots.items():
            if code not in pool_codes:
                continue
            ctx = strat.StockContext(
                code=code, name=snap.get("name", ""), snap=snap,
                daily=self.daily_cache.get(code, []),
                params=self.engine.params,
            )
            for hit in self.engine.evaluate(ctx, kind="snapshot"):
                await self._emit_signal(ctx, hit)
                hits.append({"code": code, "name": ctx.name, **hit})
        return hits

    # ------------------------------------------------------------------
    # K线增量同步
    # ------------------------------------------------------------------

    async def _kline_loop(self):
        sync_count = 0
        while self._running:
            trading = is_trading_time()
            interval = config.KLINE_SYNC_INTERVAL if trading else config.OFFLINE_KLINE_SYNC_INTERVAL
            stocks = self.store.get_stocks()
            codes = [c for c, _ in stocks]
            sync_count += 1
            # 分钟K线增量（盘中为主）
            if trading:
                k1m = await self.ds.fetch_many_kline(
                    codes, "1m", config.KLINE_FETCH_COUNT, config.KLINE_CONCURRENCY)
                for code, kl in k1m.items():
                    self.store.upsert_klines(code, kl, "kline_min")
            # 日K增量（盘中当日K线实时更新，需周期性刷新）
            if trading and sync_count % 2 == 0 or not trading:
                daily = await self.ds.fetch_many_kline(
                    codes, "daily", config.KLINE_FETCH_COUNT, config.KLINE_CONCURRENCY)
                for code, kl in daily.items():
                    self.store.upsert_klines(code, kl, "kline_daily")
                # 刷新日K缓存，供量比/RSI等实时策略使用
                for code in daily:
                    self.daily_cache[code] = self.store.get_klines(code, "kline_daily", 400)
            # 60分钟K增量（MACD金叉策略依赖，盘中每2轮同步一次，检测延迟≤60秒）
            if trading and sync_count % 2 == 0:
                k60 = await self.ds.fetch_many_kline(
                    codes, "60m", config.KLINE_FETCH_COUNT, config.KLINE_CONCURRENCY)
                for code, kl in k60.items():
                    self.store.upsert_klines(code, kl, "kline_min")
                    self.k60_cache[code] = self.store.get_klines(code, "kline_min", 400)
            self.last_kline_sync_at = time.time()
            # K线类策略扫描（休市时K线不变，跳过避免同一信号反复上报）
            if trading:
                await self._scan_kline_strategies()
            await self._broadcast_status("K线增量同步完成")
            await asyncio.sleep(interval)

    async def _scan_kline_strategies(self):
        pool_codes = {c for c, _ in self.store.get_stocks()}
        for code, snap in self.snapshots.items():
            if code not in pool_codes:
                continue
            ctx = strat.StockContext(
                code=code, name=snap.get("name", ""), snap=snap,
                daily=self.daily_cache.get(code, []),
                k60=self.k60_cache.get(code, []),
                params=self.engine.params,
            )
            for hit in self.engine.evaluate(ctx, kind="kline"):
                await self._emit_signal(ctx, hit)

    # ------------------------------------------------------------------
    # 信号与广播
    # ------------------------------------------------------------------

    async def _emit_signal(self, ctx: strat.StockContext, hit: dict):
        """信号去重 → 入库 → 广播"""
        last_ts = self.store.last_signal_time(ctx.code, hit["key"])
        if last_ts and time.time() - last_ts < config.SIGNAL_DEDUP_SECONDS:
            return
        self.store.add_signal(
            ctx.code, ctx.name, hit["key"], hit["price"], hit["change_pct"], hit["reason"])
        await self._broadcast({
            "type": "signal",
            "data": {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "code": ctx.code, "name": ctx.name,
                "strategy": hit["key"], "strategy_name": hit["name"],
                "price": hit["price"], "change_pct": hit["change_pct"],
                "reason": hit["reason"],
            },
        })

    async def _broadcast_status(self, msg: str):
        await self._broadcast({
            "type": "status",
            "data": {
                "msg": msg,
                "trading": is_trading_time(),
                "pool_size": len(self.store.get_stocks()),
                "snapshot_ok": self.snapshot_ok,
                "snapshot_fail": self.snapshot_fail,
                "last_snapshot_at": self.last_snapshot_at,
                "last_kline_sync_at": self.last_kline_sync_at,
                "strategies": self.engine.list(),
                "uptime": int(time.time() - self.started_at),
            },
        })

    async def _broadcast(self, event: dict):
        try:
            await self.emit(event)
        except Exception:
            pass
