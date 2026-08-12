# -*- coding: utf-8 -*-
"""回测引擎（固定流程）：数据加载 → 统一时间轴 → 事件循环（引擎风控 + 策略信号）→ 撮合 → 期末平仓

固定流程说明：
- 引擎负责数据、时间轴、撮合、通用风控（止盈/止损/持有期）与资金核算
- 策略只负责 on_bar 信号，通过 app.backtest.strategy 接口扩展
"""
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.backtest.cache import KlineCache
from app.backtest.loader import (FETCH_THROTTLE_SECONDS, PERIOD_BARS_PER_DAY,
                                 PERIOD_FETCH_CAP, load_kline_merged)
from app.backtest.metrics import compute_metrics
from app.backtest.position import Position, Trade
from app.backtest.rules import build_strategy
from app.backtest.strategy import BarContext, Signal, STRATEGY_REGISTRY
from app.datasource import DataSource
from app.store import Store

_ASSUMPTIONS = [
    "K线为前复权数据，忽略分红除权影响",
    "不考虑涨停无法买入、跌停无法卖出、停牌等现实约束",
    "严格执行 A股 T+1：买入当日不可卖出，卖出信号顺延至下一交易日（下一根K线）执行",
    "不做 100 股整手约束，股数可为小数",
    "止盈/止损按盘中高低价触及、触发价成交，基准为买入实际价（含滑点）",
    "回测结束仍未平仓的持仓按最后收盘价强制平仓",
]


@dataclass
class BacktestConfig:
    """回测配置（与 config.BACKTEST_DEFAULT 同构，字段缺省取默认值）"""
    scope: str = "pool"                    # pool=股票池 / watch=自选 / codes=指定代码
    codes: List[str] = field(default_factory=list)   # scope=codes 时生效
    start: str = ""                        # 回测起点 YYYY-MM-DD，空=数据起点
    end: str = ""                          # 回测终点 YYYY-MM-DD，空=数据最后一天
    period: str = "30m"                    # K线周期 1m/5m/15m/30m/60m/daily/weekly
    lookback: int = 300                    # 指标预热根数（自动向前多拉，不参与回测区间）
    init_cash: float = 1_000_000.0         # 初始资金
    commission_rate: float = 0.001         # 手续费（买卖双向）
    slippage_rate: float = 0.002           # 滑点（买价上浮/卖价下浮）
    strategy: str = "all_in_all_out"       # 整包战法 key（buy_rule/sell_rule 为空时生效）
    buy_rule: str = ""                    # 买入规则 key（组合模式）
    sell_rule: str = ""                   # 卖出规则 key（组合模式）
    params: Dict = field(default_factory=dict)     # 战法参数覆盖
    take_profit_pct: float = 0.0           # 止盈 %，0=关闭
    stop_loss_pct: float = 0.0             # 止损 %，0=关闭
    max_hold_bars: int = 0                 # 持有期上限（K线根数），0=不限制
    max_positions: int = 0                 # 最大同时持仓数（0=不限制）
    exec_price: str = "close"              # close=信号当根收盘 / next_open=下一根开盘
    min_bars: int = 60                     # 单只股票最小数据根数，不足跳过并警告
    concurrency: int = 10                  # K线拉取并发数

    @classmethod
    def from_dict(cls, d: dict) -> "BacktestConfig":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in keys})


class BacktestEngine:
    """固定流程回测引擎"""

    def __init__(self, cfg: BacktestConfig):
        self.cfg = cfg
        self.ds = DataSource()
        self.store = Store()
        self.cache = KlineCache()
        self.cash = cfg.init_cash
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[dict] = []
        self.strategies: Dict[str, object] = {}   # 每只股票独立的策略实例
        self.last_price: Dict[str, float] = {}    # 各股票最近收盘价（净值核算）
        self.pending: Dict[str, Signal] = {}      # next_open 模式延迟信号
        self.bars_map: Dict[str, List[dict]] = {} # 预处理后的K线（供策略钩子取用）
        self.warnings: List[str] = []             # 数据覆盖不足等警告

    # ------------------------- 主流程 -------------------------

    async def run(self) -> dict:
        cfg = self.cfg
        stocks = self._resolve_scope()
        if not stocks:
            return self._result(ok=False, msg="标的列表为空，请检查 scope 配置")
        names = dict(stocks)

        # 数据：本地缓存优先（长期复用）；缺失区间增量拉取合并，
        # 逐只串行（每次只请求一支股票）间隔节流防限速
        need = self._estimate_fetch_limit()
        warmup_start = self._warmup_start()
        data: Dict[str, List[dict]] = {}
        cache_hits = 0
        for code in list(names):
            kl, from_cache = await load_kline_merged(
                self.ds, self.cache, code, cfg.period, need, warmup_start, cfg.end)
            if from_cache and kl:
                cache_hits += 1
            if kl:
                data[code] = kl
            await asyncio.sleep(FETCH_THROTTLE_SECONDS)
        self.data_source = {"cache_hits": cache_hits, "fetched": len(data) - cache_hits}
        bars_map, skipped = self._prepare_data(data, names)
        if not bars_map:
            return self._result(ok=False, msg="无可用K线数据（数据源不可用或数据不足）",
                                skipped=skipped)
        self.bars_map = bars_map
        codes_ok = list(bars_map)

        # 统一时间轴：回测区间内所有股票K线时间点，升序
        timeline = sorted({k["ts"] for code in codes_ok
                           for k in bars_map[code] if not cfg.start or k["ts"] >= cfg.start})
        if not timeline:
            return self._result(ok=False, msg="回测区间内无K线数据，请检查 start/end",
                                skipped=skipped)
        ts_index = {code: {k["ts"]: i for i, k in enumerate(kl)}
                    for code, kl in bars_map.items()}

        # 数据覆盖警告：数据源（尤其分钟K）可能只返回最近一段，实际回测区间会被压缩
        # 偏差超过 7 天才提示，避免节假日导致的交易日错位误报
        try:
            gap_start = (datetime.strptime(timeline[0][:10], "%Y-%m-%d")
                         - datetime.strptime(cfg.start, "%Y-%m-%d")).days if cfg.start else 0
        except ValueError:
            gap_start = 0
        if gap_start > 7:
            self.warnings.append(
                f"数据源仅覆盖 {timeline[0]} 起，实际回测起点为 {timeline[0]}（配置为 {cfg.start}）")
        if cfg.end and timeline[-1] < cfg.end:
            self.warnings.append(
                f"数据源最新到 {timeline[-1]}，实际回测终点为 {timeline[-1]}（配置为 {cfg.end}）")

        # 每只股票独立的策略实例（状态隔离）：reset + prepare 预计算指标
        for code in codes_ok:
            st = build_strategy(cfg)
            st.prepare(bars_map[code])
            self.strategies[code] = st

        # 事件循环：逐时间点推进所有股票
        for ts in timeline:
            for code in codes_ok:
                idx = ts_index[code].get(ts)
                if idx is None:
                    continue
                bar = bars_map[code][idx]
                self.last_price[code] = bar["close"]

                # 0. 上一根K线的延迟信号（next_open 模式按本根开盘价成交）
                sig = self.pending.pop(code, None)
                if sig is not None:
                    if sig.action == "buy" and code not in self.positions \
                            and (cfg.max_positions <= 0
                                 or len(self.positions) < cfg.max_positions):
                        self._buy(code, names[code], bar, sig, idx, price=bar["open"])
                    elif sig.action == "sell" and code in self.positions:
                        pos = self.positions[code]
                        if bar["ts"][:10] > pos.buy_ts[:10]:
                            # T+1 生效：下一交易日可卖，按本根开盘价成交；
                            # 止损单若跳空低开（开盘价 < 触发价）按开盘价成交，避免高估卖出价
                            if sig.price is not None:
                                base = min(sig.price, bar["open"])
                                sig = Signal(sig.action, sig.reason, sig.fraction,
                                             sig.amount, base)
                            self._sell(code, names[code], bar, pos, sig, idx)
                        else:
                            # 仍为买入当日（分钟K同日后续K线），继续顺延
                            self.pending[code] = sig

                pos = self.positions.get(code)
                # 1. 引擎级通用风控（止盈/止损/持有期），优先于策略信号
                risk = self._check_risk(bar, idx, pos)
                if risk is not None:
                    self._sell(code, names[code], bar, pos, risk, idx)
                    continue

                # 2. 策略信号
                st = self.strategies[code]
                ctx = BarContext(code, names[code], bars_map[code], idx, pos, st.params)
                sig = st.on_bar(ctx)
                if sig is None or sig.action not in ("buy", "sell"):
                    continue
                if sig.action == "buy":
                    if pos is None:
                        if cfg.max_positions > 0 and len(self.positions) >= cfg.max_positions:
                            pass  # 已达最大持仓数上限，跳过本次买入
                        elif cfg.exec_price == "next_open":
                            self.pending[code] = sig      # 延迟到下一根开盘价成交
                        else:
                            self._buy(code, names[code], bar, sig, idx)
                elif sig.action == "sell" and pos is not None:
                    if cfg.exec_price == "next_open" or bar["ts"][:10] <= pos.buy_ts[:10]:
                        # next_open 延迟成交；或 T+1 买入当日不可卖，顺延至下一交易日
                        self.pending[code] = sig
                    else:
                        self._sell(code, names[code], bar, pos, sig, idx)

            # 每根K线结束后记录组合净值
            pos_value = sum(self.positions[c].shares * self.last_price[c]
                            for c in self.positions)
            self.equity_curve.append({"ts": ts, "value": round(self.cash + pos_value, 2),
                                      "cash": round(self.cash, 2),
                                      "position_value": round(pos_value, 2)})

        # 收尾：期末强制平仓（按各股票最后一根K线收盘价）
        for code, pos in list(self.positions.items()):
            last_bar = bars_map[code][-1]
            sig = Signal("sell", reason="期末强制平仓", price=last_bar["close"])
            self._sell(code, names[code], last_bar, pos, sig, len(bars_map[code]) - 1)

        metrics = compute_metrics(self.equity_curve, self.trades, cfg.init_cash)
        return self._result(ok=True, stocks=stocks, skipped=skipped, metrics=metrics)

    # ------------------------- 数据准备 -------------------------

    def _resolve_scope(self) -> List[Tuple[str, str]]:
        """解析标的范围：pool=股票池 / watch=自选 / codes=指定代码"""
        cfg = self.cfg
        if cfg.scope == "watch":
            return self.store.get_watch()
        if cfg.scope == "codes":
            known = {c: n for c, n in self.store.get_stocks()}
            known.update({c: n for c, n in self.store.get_watch()})
            return [(c, known.get(c, c)) for c in cfg.codes if c]
        return self.store.get_stocks()

    def _estimate_fetch_limit(self) -> int:
        """按回测区间 + 预热估算需拉取的K线根数"""
        cfg = self.cfg
        bpd = PERIOD_BARS_PER_DAY.get(cfg.period, 8)
        days = 250
        if cfg.start:
            try:
                s = datetime.strptime(cfg.start, "%Y-%m-%d")
                e = datetime.strptime(cfg.end, "%Y-%m-%d") if cfg.end else datetime.now()
                days = max((e - s).days, 0)
            except ValueError:
                pass
        need = int(cfg.lookback + days * bpd * 1.3) + 10
        cap = PERIOD_FETCH_CAP.get(cfg.period, 8000)
        return min(max(need, 100), cap)

    def _warmup_start(self) -> str:
        """预热起点：回测起点向前推 lookback 根K线所需自然日"""
        cfg = self.cfg
        if not cfg.start:
            return ""
        bpd = PERIOD_BARS_PER_DAY.get(cfg.period, 8)
        try:
            d = datetime.strptime(cfg.start, "%Y-%m-%d") - timedelta(days=int(cfg.lookback / bpd) + 10)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            return ""

    def _prepare_data(self, data: Dict[str, List[dict]],
                      names: Dict[str, str]) -> Tuple[Dict[str, List[dict]], List[dict]]:
        """过滤预热/回测区间，剔除数据不足或无区间K线的股票"""
        cfg = self.cfg
        warmup_start = self._warmup_start()
        bars_map: Dict[str, List[dict]] = {}
        skipped: List[dict] = []
        for code, name in names.items():
            kl = data.get(code) or []
            kl = [k for k in kl if k["ts"] >= warmup_start
                  and (not cfg.end or k["ts"] <= cfg.end)]
            if not kl:
                skipped.append({"code": code, "name": name,
                                "reason": "数据拉取失败（数据源不可用）"})
                continue
            if len(kl) < cfg.min_bars:
                skipped.append({"code": code, "name": name,
                                "reason": f"有效K线仅 {len(kl)} 根（需 ≥{cfg.min_bars}）"})
                continue
            if cfg.start and not any(k["ts"] >= cfg.start for k in kl):
                skipped.append({"code": code, "name": name,
                                "reason": "回测区间内无K线（停牌或数据缺失）"})
                continue
            bars_map[code] = kl
        return dict(sorted(bars_map.items())), skipped

    # ------------------------- 风控与撮合 -------------------------

    def _check_risk(self, bar: dict, idx: int, pos: Optional[Position]) -> Optional[Signal]:
        """引擎级通用风控：止损 > 止盈 > 持有期（同根同时触及取更不利者）
        A股 T+1：买入当日不可卖出，风控同日不触发，次日重新评估"""
        cfg = self.cfg
        if pos is None:
            return None
        if bar["ts"][:10] <= pos.buy_ts[:10]:
            return None
        if cfg.stop_loss_pct > 0:
            sl = pos.buy_price * (1 - cfg.stop_loss_pct / 100.0)
            if bar["low"] <= sl:
                return Signal("sell", price=sl, reason=f"止损触发 -{cfg.stop_loss_pct}%")
        if cfg.take_profit_pct > 0:
            tp = pos.buy_price * (1 + cfg.take_profit_pct / 100.0)
            if bar["high"] >= tp:
                return Signal("sell", price=tp, reason=f"止盈触发 +{cfg.take_profit_pct}%")
        if cfg.max_hold_bars > 0 and idx - pos.entry_bar_index >= cfg.max_hold_bars:
            return Signal("sell", price=bar["close"],
                          reason=f"持有期 {cfg.max_hold_bars} 根到期")
        return None

    def _buy(self, code: str, name: str, bar: dict, sig: Signal, idx: int,
             price: Optional[float] = None) -> None:
        """买入撮合：按信号价（默认当根收盘）+ 滑点成交，扣除手续费
        金额：Signal.amount 固定金额优先，否则按 fraction 比例；现金不足按剩余买入"""
        cfg = self.cfg
        base = price if price is not None else (sig.price if sig.price is not None else bar["close"])
        exec_price = base * (1 + cfg.slippage_rate)
        # 价格无效（停牌等脏数据）不可成交，否则下面 amount/exec_price 会除零
        if exec_price <= 0:
            return
        if sig.amount and sig.amount > 0:
            amount = min(sig.amount, self.cash)
        else:
            amount = self.cash * sig.fraction
        if amount <= 0:
            return
        commission = amount * cfg.commission_rate
        shares = amount / exec_price
        pos = Position(code=code, name=name, buy_ts=bar["ts"], buy_price=exec_price,
                       shares=shares, cost=amount + commission,
                       entry_bar_index=idx, buy_reason=sig.reason)
        self.cash -= amount + commission
        self.positions[code] = pos
        st = self.strategies.get(code)
        if st is not None:
            st.on_position_opened(BarContext(code, name, self.bars_map.get(code) or [],
                                             idx, pos, st.params), pos)

    def _sell(self, code: str, name: str, bar: dict, pos: Position, sig: Signal,
              idx: int) -> None:
        """卖出撮合：按信号价（默认当根收盘/触发价）- 滑点成交，扣除手续费并结算"""
        cfg = self.cfg
        base = sig.price if sig.price is not None else bar["close"]
        exec_price = base * (1 - cfg.slippage_rate)
        amount = pos.shares * exec_price
        commission = amount * cfg.commission_rate
        pnl = amount - commission - pos.cost
        pnl_pct = pnl / pos.cost * 100.0 if pos.cost else 0.0
        trade = Trade(code=code, name=name, buy_ts=pos.buy_ts, buy_price=pos.buy_price,
                      sell_ts=bar["ts"], sell_price=exec_price, shares=pos.shares,
                      pnl=pnl, pnl_pct=pnl_pct, hold_bars=idx - pos.entry_bar_index,
                      buy_reason=pos.buy_reason, sell_reason=sig.reason)
        self.cash += amount - commission
        self.trades.append(trade)
        del self.positions[code]
        st = self.strategies.get(code)
        if st is not None:
            st.on_position_closed(BarContext(code, name, self.bars_map.get(code) or [],
                                             idx, None, st.params), pos)

    # ------------------------- 结果 -------------------------

    def _result(self, ok: bool, msg: str = "", stocks: Optional[list] = None,
                skipped: Optional[list] = None, metrics: Optional[dict] = None) -> dict:
        meta = STRATEGY_REGISTRY.get(self.cfg.strategy)
        # 组合模式：优先取策略实例名称（买入规则+卖出规则）
        st = next(iter(self.strategies.values()), None)
        st_name = getattr(st, "name", "") if st else ""
        st_desc = getattr(st, "desc", "") if st else ""
        return {
            "ok": ok,
            "msg": msg,
            "config": asdict(self.cfg),
            "strategy": {"key": self.cfg.strategy,
                         "name": st_name or (meta.name if meta else ""),
                         "desc": st_desc or (meta.desc if meta else "")},
            "stocks": [{"code": c, "name": n} for c, n in (stocks or [])],
            "skipped": skipped or [],
            "warnings": self.warnings,
            "data_source": getattr(self, "data_source", {}),
            "equity_curve": self.equity_curve,
            "trades": [asdict(t) for t in self.trades],
            "metrics": metrics or {},
            "assumptions": _ASSUMPTIONS,
        }


async def run_backtest(cfg) -> dict:
    """运行回测（cfg 为 BacktestConfig 或 dict），自动释放数据源连接"""
    if isinstance(cfg, dict):
        cfg = BacktestConfig.from_dict(cfg)
    engine = BacktestEngine(cfg)
    try:
        return await engine.run()
    finally:
        await engine.ds.close()
