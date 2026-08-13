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
        # K线缓存：period -> code -> 升序K线。按周期隔离，
        # 早先 daily_cache/k60_cache 两个 dict 读的是同一张混存表，60m 序列被 1m 污染
        self.bars: Dict[str, Dict[str, List[dict]]] = {
            p: {} for p in config.REALTIME_PERIODS}
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
        """首次启动逐周期拉取历史K线，失败的股票逐个重试补齐"""
        stocks = self.store.get_stocks()
        codes = [c for c, _ in stocks]
        loaded = {}
        for period, cfg in config.REALTIME_PERIODS.items():
            data = await self.ds.fetch_many_kline(
                codes, period, cfg["history"], config.KLINE_CONCURRENCY)
            # 补齐拉取失败的股票（单个串行重试，降低被限流概率）
            for code in codes:
                if code not in data:
                    kl = await self.ds.kline(code, period, cfg["history"])
                    if kl:
                        data[code] = kl
            for code, kl in data.items():
                self.store.upsert_klines(code, kl, period)
            for code in codes:
                self.bars[period][code] = self.store.get_klines(code, period, 400)
            loaded[period] = len(data)
        await self._broadcast({
            "type": "status",
            "data": {"msg": "历史K线加载完成：" + " / ".join(
                f"{p} {n} 只" for p, n in loaded.items())},
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
            daily = self.bars["daily"].get(code, [])
            if len(daily) <= 5:
                daily = self.store.get_klines(code, "daily", 400)
                if daily:
                    self.bars["daily"][code] = daily
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
                daily=self.bars["daily"].get(code, []),
                bars=self._bars_of(code),
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
            sync_count += 1
            # 整体兜底：这里任何异常若逃逸出去，task 会永久静默死亡
            # （K线同步与全部K线策略一起停，而快照循环仍在跑，UI 看着完全正常）
            try:
                synced = await self._sync_klines(trading, sync_count)
                self.last_kline_sync_at = time.time()
                # K线类策略扫描（休市时K线不变，跳过避免同一信号反复上报）
                if trading:
                    await self._scan_kline_strategies()
                await self._broadcast_status(
                    "K线增量同步完成" + (f"：{', '.join(synced)}" if synced else "（本轮无周期到期）"))
            except Exception as e:
                await self._broadcast_status(f"K线同步异常：{type(e).__name__}: {e}")
            await asyncio.sleep(interval)

    async def _sync_klines(self, trading: bool, sync_count: int) -> List[str]:
        """按 config.REALTIME_PERIODS 逐周期增量同步，返回本轮实际同步的周期"""
        codes = [c for c, _ in self.store.get_stocks()]
        synced: List[str] = []
        for offset, (period, cfg) in enumerate(config.REALTIME_PERIODS.items()):
            if trading:
                # 错峰：同一轮不把所有周期都打出去，按周期序号偏移
                if sync_count % cfg["every"] != offset % cfg["every"]:
                    continue
            elif not cfg["offline"]:
                continue
            data = await self.ds.fetch_many_kline(
                codes, period, config.KLINE_FETCH_COUNT, config.KLINE_CONCURRENCY)
            for code, kl in data.items():
                self.store.upsert_klines(code, kl, period)
                self.bars[period][code] = self.store.get_klines(code, period, 400)
            synced.append(f"{period} {len(data)} 只")
        return synced

    async def _scan_kline_strategies(self):
        pool_codes = {c for c, _ in self.store.get_stocks()}
        for code, snap in self.snapshots.items():
            if code not in pool_codes:
                continue
            ctx = strat.StockContext(
                code=code, name=snap.get("name", ""), snap=snap,
                daily=self.bars["daily"].get(code, []),
                k60=self.bars["60m"].get(code, []),
                bars=self._bars_of(code),
            )
            for hit in self.engine.evaluate(ctx, kind="kline"):
                await self._emit_signal(ctx, hit)

    def _bars_of(self, code: str) -> Dict[str, List[dict]]:
        """该股各周期K线，供按周期取用（回测规则适配器用得上）"""
        return {p: self.bars[p].get(code, []) for p in self.bars}

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
