# -*- coding: utf-8 -*-
"""后台历史数据下载：逐只（逐周期）把长历史K线灌入回测缓存 kline_cache

与实时数据严格分离，两者用途不同、互不写入：
- 本模块只写 kline_cache（按 (code, period, ts) 隔离每个周期）→ 供战法验证/回测
- 实时系统的 kline_daily / kline_min 由 Scanner 写入 → 供盘中实时分析
  （kline_min 把 1m 与 60m 混存一张表，且用 INSERT OR IGNORE 不修正未收盘K线，
   这两点对回测都是错的，所以回测数据必须走独立的 kline_cache）

数据源固定 BaoStock，不走 DataSource.kline 的四级降级链，原因：
- 腾讯/新浪不认日期参数（只认 limit 返回最近 N 根）；东财只认 start_date，
  end 硬编码 20500101，拉不到指定历史窗口
- BaoStock 同时认 start_date+end_date，且给了 start_date 就无视 limit，
  一次查询返回整个区间全部根数，不受 PERIOD_FETCH_CAP 约束
- 只有 BaoStock 的 amount 是真实成交额（腾讯/新浪硬编码 0.0），落缓存不脏数据

进度通过 on_progress 回调外抛，本模块不做任何打印，
便于 CLI / 未来的 WebSocket 推送共用同一套编排逻辑
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from app.backtest.cache import KlineCache
from app.backtest.loader import _COVER_TOLERANCE_DAYS, day_gap
from app.datasource import BaoStockKline, DataSource
from app.store import Store

# BaoStock 支持的周期（BS_PERIODS 无 1m：分钟线最细 5m）
BAOSTOCK_PERIODS = ("5m", "15m", "30m", "60m", "daily", "weekly")

# 头部缺口容差（自然日）：沿用 loader 的 7 天。头部缺口要整段重下，代价高，
# 不能因节假日错位误触发
_HEAD_TOLERANCE_DAYS = _COVER_TOLERANCE_DAYS
# 尾部缺口容差（自然日）：0 = 末根只要早于 end 就去补，让每日增量更新真正生效。
# 非交易日必然拉空，靠「尾部拉空不算失败」兜住（见 _one_task）
_TAIL_TOLERANCE_DAYS = 0

# 冷却等待时的轮询间隔（秒）：每次抛一个 cooldown 事件供调用方刷倒计时
_COOLDOWN_POLL_SECONDS = 5.0

ProgressCallback = Callable[[dict], None]


@dataclass
class DownloadConfig:
    """下载配置（与 config.DOWNLOAD_DEFAULT 同构，字段缺省取默认值）"""
    scope: str = "pool"                    # pool=股票池 / watch=自选 / all=全市场 / codes=指定
    codes: List[str] = field(default_factory=list)      # scope=codes 时生效
    periods: List[str] = field(default_factory=lambda: ["30m", "daily"])
    start: str = "2020-01-01"              # 下载起点 YYYY-MM-DD
    end: str = ""                          # 下载终点 YYYY-MM-DD，空=今天
    mode: str = "incremental"              # incremental=只补缺口 / full=整段重拉覆盖
    throttle: float = 0.5                  # 每个任务之间的间隔（秒）
    force: bool = False                    # 先清缓存再下（隐含 full）
    wait_cooldown: bool = True             # 数据源冷却时等待（False=直接跳过记失败）

    @classmethod
    def from_dict(cls, d: dict) -> "DownloadConfig":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in keys})


class Downloader:
    """逐只逐周期下载/更新回测缓存

    on_progress 会收到以下 type 的事件（均为 dict）：
    - start     总任务数与区间，队列启动时一次
    - task      单个任务完成（status=ok/cached/failed）
    - cooldown  数据源冷却等待中，每 _COOLDOWN_POLL_SECONDS 一次
    - done      全部完成，附汇总
    """

    def __init__(self, cfg: DownloadConfig, on_progress: Optional[ProgressCallback] = None):
        self.cfg = cfg
        self.ds = DataSource()
        self.store = Store()
        self.cache = KlineCache()
        self.on_progress = on_progress
        self.end = cfg.end or time.strftime("%Y-%m-%d")
        self.failures: List[dict] = []

    # ------------------------- 主流程 -------------------------

    async def run(self) -> dict:
        try:
            return await self._run()
        finally:
            await self.ds.close()

    async def _run(self) -> dict:
        cfg = self.cfg
        started = time.time()

        bad = [p for p in cfg.periods if p not in BAOSTOCK_PERIODS]
        if bad:
            return self._result(ok=False, elapsed=0.0, msg=(
                f"BaoStock 不支持周期 {', '.join(bad)}；"
                f"可用周期：{', '.join(BAOSTOCK_PERIODS)}"))
        if not cfg.periods:
            return self._result(ok=False, elapsed=0.0, msg="周期列表为空")

        stocks = await self._resolve_scope()
        if not stocks:
            return self._result(ok=False, elapsed=0.0,
                                msg="标的列表为空，请检查 scope / codes 配置")

        # 股票外层、周期内层：同一只的多个周期连续下载，日志更连贯
        tasks = [(code, name, period)
                 for code, name in stocks for period in cfg.periods]
        self._emit({"type": "start", "total": len(tasks), "stocks": len(stocks),
                    "periods": list(cfg.periods), "start": cfg.start, "end": self.end,
                    "scope": cfg.scope, "mode": cfg.mode})

        ok = cached = nodata = failed = added_bars = 0
        for i, (code, name, period) in enumerate(tasks, 1):
            ev = await self._one_task(code, name, period)
            ev.update({"type": "task", "index": i, "total": len(tasks)})
            status = ev["status"]
            if status == "ok":
                ok += 1
                added_bars += ev["added"]
            elif status == "cached":
                cached += 1
            elif status == "nodata":
                nodata += 1
            else:
                failed += 1
                self.failures.append({"code": code, "name": name, "period": period,
                                      "reason": ev["msg"]})
            self._emit(ev)

        return self._result(ok=True, elapsed=time.time() - started, total=len(tasks),
                            ok_count=ok, cached=cached, nodata=nodata, failed=failed,
                            added_bars=added_bars)

    # ------------------------- 单个任务 -------------------------

    async def _one_task(self, code: str, name: str, period: str) -> dict:
        """下载/更新单只单周期。异常一律兜成 failed，绝不中断整队"""
        t0 = time.time()
        ev = {"code": code, "name": name, "period": period, "status": "failed",
              "added": 0, "bars": 0, "refetched": 0, "elapsed": 0.0, "msg": ""}
        try:
            if self.cfg.force:
                self.cache.clear(code, period)

            before, first_ts, last_ts = self.cache.stat(code, period)
            span = self._missing_span(before, first_ts, last_ts)
            if span is None:
                ev.update({"status": "cached", "bars": before,
                           "elapsed": time.time() - t0})
                return ev   # 已覆盖：不联网、不节流

            await self._wait_cooldown()
            if not BaoStockKline._available():
                ev.update({"msg": "数据源冷却中，已跳过", "elapsed": time.time() - t0})
                return ev

            fetch_start, fetch_end, kind = span
            # limit 传 0：BaoStock 在给了 start_date 时完全忽略 limit
            kl = await BaoStockKline.fetch_kline(
                code, period, limit=0, start_date=fetch_start, end_date=fetch_end)
            if kl:
                self.cache.put(code, period, kl)
            after, _, _ = self.cache.stat(code, period)

            if kl:
                # 尾部区间刻意与末根重叠一天以修正未收盘K线，所以「拉到数据」
                # 不等于「有新数据」；以实际入库根数变化为准，避免虚报更新
                added = after - before
                ev.update({"status": "ok" if added > 0 else "nodata",
                           "added": added, "bars": after, "refetched": len(kl)})
                if added <= 0:
                    ev["msg"] = "无新增（已是最新）"
            elif not BaoStockKline._available():
                # 区分「数据源刚被熔断」与「确实没数据」，避免误报
                ev["msg"] = "拉取超时触发熔断"
            elif kind == "tail":
                # 尾部增量拉空：非交易日或尚未收盘，不是失败
                ev.update({"status": "nodata", "bars": before,
                           "msg": f"无新数据（{fetch_start}~{fetch_end}）"})
            else:
                ev["msg"] = f"数据源返回空（{fetch_start}~{fetch_end}）"
            await asyncio.sleep(self.cfg.throttle)
        except Exception as e:
            ev["msg"] = f"{type(e).__name__}: {e}"
        ev["elapsed"] = time.time() - t0
        return ev

    def _missing_span(self, bars: int, first_ts: str, last_ts: str
                      ) -> Optional[Tuple[str, str, str]]:
        """算出需要拉取的区间；已完全覆盖返回 None

        返回 (起, 止, kind)，kind 决定「拉空」怎么判：
        - full：全量/头部缺口，拉空 = 真失败
        - tail：尾部增量，拉空 = 无新数据（非交易日/尚未收盘），不算失败
        """
        start = self.cfg.start
        if bars == 0 or self.cfg.mode == "full":
            return start, self.end, "full"
        head_gap = bool(start) and day_gap(first_ts, start) > _HEAD_TOLERANCE_DAYS
        tail_gap = day_gap(self.end, last_ts) > _TAIL_TOLERANCE_DAYS
        if not head_gap and not tail_gap:
            return None
        if head_gap:
            # 头部有缺口就整段重下：BaoStock 一次能返回全区间，
            # put 是主键 upsert 重复根数覆盖无害，比拼接两段简单可靠
            return start, self.end, "full"
        # 尾部从「末根当天」而非次日开始：末根可能是盘中拉的未收盘K线，
        # 重叠一天让 upsert 用最终值覆盖它（成本仅多一天的根数）
        return last_ts[:10], self.end, "tail"

    async def _wait_cooldown(self) -> None:
        """等 BaoStock 熔断冷却结束

        冷却期内所有请求秒返空，若按「失败即跳过」处理，一次熔断会把剩余
        上百只全部误判为失败。等待期间不消耗任务、不记失败
        """
        if not self.cfg.wait_cooldown:
            return
        while not BaoStockKline._available():
            remain = max(0.0, BaoStockKline._cooldown_until - time.time())
            if remain <= 0:
                break
            self._emit({"type": "cooldown", "remain": remain})
            await asyncio.sleep(min(_COOLDOWN_POLL_SECONDS, remain))

    # ------------------------- 辅助 -------------------------

    async def _resolve_scope(self) -> List[Tuple[str, str]]:
        """解析标的范围：pool=股票池 / watch=自选 / all=全市场 / codes=指定代码"""
        cfg = self.cfg
        if cfg.scope == "watch":
            return self.store.get_watch()
        if cfg.scope == "all":
            return await self.ds.all_stocks()
        if cfg.scope == "codes":
            known = {c: n for c, n in self.store.get_stocks()}
            known.update({c: n for c, n in self.store.get_watch()})
            return [(c, known.get(c, c)) for c in cfg.codes if c]
        return self.store.get_stocks()

    def _emit(self, event: dict) -> None:
        """抛进度事件；回调异常不影响下载（与 Scanner._broadcast 同策略）"""
        if not self.on_progress:
            return
        try:
            self.on_progress(event)
        except Exception:
            pass

    def _result(self, ok: bool, elapsed: float, msg: str = "", total: int = 0,
                ok_count: int = 0, cached: int = 0, nodata: int = 0, failed: int = 0,
                added_bars: int = 0) -> dict:
        result = {"ok": ok, "msg": msg, "total": total, "ok_count": ok_count,
                  "cached": cached, "nodata": nodata, "failed": failed,
                  "added_bars": added_bars,
                  "elapsed": elapsed, "failures": self.failures,
                  "start": self.cfg.start, "end": self.end,
                  "periods": list(self.cfg.periods), "scope": self.cfg.scope,
                  "mode": self.cfg.mode}
        self._emit({"type": "done", **result})
        return result


async def run_download(cfg, on_progress: Optional[ProgressCallback] = None) -> dict:
    """下载入口：cfg 可以是 DownloadConfig 或 dict"""
    if isinstance(cfg, dict):
        cfg = DownloadConfig.from_dict(cfg)
    return await Downloader(cfg, on_progress).run()
