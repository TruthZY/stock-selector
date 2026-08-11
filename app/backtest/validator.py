# -*- coding: utf-8 -*-
"""信号验证器：不模拟资金池与持仓数量，每次出手按固定金额独立核算盈亏

用途：快速验证战法信号本身的质量——所有买入信号都按 amount 出手（无现金不足、
无持仓上限概念），逐笔统计盈亏，输出总盈亏/胜率/买卖时机清单。

与 BacktestEngine 的关系：复用同一套 BaseStrategy 战法接口与数据链路（缓存+
串行拉取），但撮合模型简化为"单笔独立核算"，不生成资金曲线。
"""
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.backtest.cache import KlineCache
from app.backtest.engine import (FETCH_THROTTLE_SECONDS, PERIOD_BARS_PER_DAY,
                                 PERIOD_FETCH_CAP)
from app.backtest.strategy import (STRATEGY_REGISTRY, BarContext, Signal,
                                   get_strategy)
from app.datasource import DataSource
from app.store import Store

_ASSUMPTIONS = [
    "每笔按固定金额独立核算，不模拟资金池与持仓上限",
    "K线为前复权数据，忽略分红除权影响",
    "严格执行 A股 T+1：买入当日不可卖出，卖出信号顺延至下一交易日",
    "止盈/止损按盘中高低价触及、触发价成交，基准为买入实际价（含滑点）",
    "回测结束仍未平仓的持仓按最后收盘价强制平仓",
]


@dataclass
class ValidatorConfig:
    """验证器配置（前端表单参数，与 config.BACKTEST_DEFAULT 对齐）"""
    scope: str = "codes"                    # pool=股票池 / watch=自选 / codes=指定代码
    codes: List[str] = field(default_factory=list)
    start: str = ""                         # YYYY-MM-DD，空=数据起点
    end: str = ""                           # YYYY-MM-DD，空=数据最后一天
    period: str = "30m"
    lookback: int = 300
    amount: float = 10000.0                 # 每次出手金额（元）
    commission_rate: float = 0.001
    slippage_rate: float = 0.002
    strategy: str = "kdj_rsi_golden"
    params: Dict = field(default_factory=dict)
    take_profit_pct: float = 0.0            # 止盈 %，0=关闭
    stop_loss_pct: float = 0.0              # 止损 %，0=关闭
    max_hold_bars: int = 0                  # 持有期上限（根），0=不限制
    exec_price: str = "close"
    min_bars: int = 60

    @classmethod
    def from_dict(cls, d: dict) -> "ValidatorConfig":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in keys})


@dataclass
class VPosition:
    """验证器持仓：字段与引擎 Position 对齐（战法以属性访问 buy_price/buy_ts 等）"""
    code: str
    name: str
    buy_ts: str
    buy_price: float            # 实际买入价（含滑点）
    amount: float               # 投入金额（固定单笔）
    commission: float           # 买入手续费
    entry_bar_index: int
    buy_reason: str = ""


class Validator:
    """信号验证器：固定金额单笔核算"""

    def __init__(self, cfg: ValidatorConfig):
        self.cfg = cfg
        self.ds = DataSource()
        self.store = Store()
        self.cache = KlineCache()
        self.warnings: List[str] = []
        self.positions: Dict[str, VPosition] = {}   # code -> 持仓
        self.trades: List[dict] = []
        self.pending: Dict[str, Signal] = {}   # T+1/next_open 延迟信号
        self.strategies: Dict[str, object] = {}

    # ------------------------- 主流程 -------------------------

    async def run(self) -> dict:
        cfg = self.cfg
        stocks = self._resolve_scope()
        if not stocks:
            return self._result(ok=False, msg="标的列表为空，请检查 scope 配置")
        names = dict(stocks)

        # 数据：缓存优先 + 逐只串行拉取（防数据源限速）
        need = self._estimate_limit()
        warmup_start = self._warmup_start()
        data: Dict[str, List[dict]] = {}
        cache_hits = 0
        for code in list(names):
            kl = self.cache.get(code, cfg.period, warmup_start, cfg.end)
            if kl is not None:
                data[code] = kl
                cache_hits += 1
                continue
            kl = await self.ds.kline(code, cfg.period, need, min_len=need,
                                     start_date=warmup_start, end_date=cfg.end)
            if kl:
                self.cache.put(code, cfg.period, kl)
                data[code] = kl
            await asyncio.sleep(FETCH_THROTTLE_SECONDS)
        bars_map, skipped = self._prepare_data(data, names)
        if not bars_map:
            return self._result(ok=False, msg="无可用K线数据（数据源不可用或数据不足）",
                                skipped=skipped)
        codes_ok = list(bars_map)

        timeline = sorted({k["ts"] for code in codes_ok
                           for k in bars_map[code] if not cfg.start or k["ts"] >= cfg.start})
        if not timeline:
            return self._result(ok=False, msg="回测区间内无K线数据，请检查 start/end",
                                skipped=skipped)
        ts_index = {code: {k["ts"]: i for i, k in enumerate(kl)}
                    for code, kl in bars_map.items()}

        # 数据覆盖警告（与引擎一致）
        try:
            gap = (datetime.strptime(timeline[0][:10], "%Y-%m-%d")
                   - datetime.strptime(cfg.start, "%Y-%m-%d")).days if cfg.start else 0
        except ValueError:
            gap = 0
        if gap > 7:
            self.warnings.append(
                f"数据源仅覆盖 {timeline[0]} 起，实际验证起点为 {timeline[0]}（配置为 {cfg.start}）")

        # 每只股票独立策略实例
        for code in codes_ok:
            st = get_strategy(cfg.strategy)
            st.params = {**st.default_params, **cfg.params}
            st.reset()
            st.prepare(bars_map[code])
            self.strategies[code] = st

        # 事件循环：逐时间点推进，单笔独立核算
        for ts in timeline:
            for code in codes_ok:
                idx = ts_index[code].get(ts)
                if idx is None:
                    continue
                bar = bars_map[code][idx]

                # 0. 延迟信号（next_open / T+1 顺延）
                sig = self.pending.pop(code, None)
                if sig is not None:
                    if sig.action == "buy" and code not in self.positions:
                        self._open(code, names[code], bar, sig, idx, price=bar["open"])
                    elif sig.action == "sell" and code in self.positions:
                        pos = self.positions[code]
                        if bar["ts"][:10] > pos.buy_ts[:10]:
                            if sig.price is not None:
                                base = min(sig.price, bar["open"])
                                sig = Signal(sig.action, sig.reason, sig.fraction,
                                             sig.amount, base)
                            self._close(code, names[code], bar, pos, sig, idx)
                        else:
                            self.pending[code] = sig

                pos = self.positions.get(code)
                # 1. 风控（止盈/止损/持有期）
                risk = self._check_risk(bar, idx, pos)
                if risk is not None:
                    self._close(code, names[code], bar, pos, risk, idx)
                    continue

                # 2. 策略信号
                st = self.strategies[code]
                ctx = BarContext(code, names[code], bars_map[code], idx, pos, st.params)
                sig = st.on_bar(ctx)
                if sig is None or sig.action not in ("buy", "sell"):
                    continue
                if sig.action == "buy":
                    if pos is None:
                        if cfg.exec_price == "next_open":
                            self.pending[code] = sig
                        else:
                            self._open(code, names[code], bar, sig, idx)
                elif sig.action == "sell" and pos is not None:
                    if cfg.exec_price == "next_open" or bar["ts"][:10] <= pos.buy_ts[:10]:
                        self.pending[code] = sig
                    else:
                        self._close(code, names[code], bar, pos, sig, idx)

        # 收尾：期末强制平仓
        for code, pos in list(self.positions.items()):
            last_bar = bars_map[code][-1]
            sig = Signal("sell", reason="期末强制平仓", price=last_bar["close"])
            self._close(code, names[code], last_bar, pos, sig, len(bars_map[code]) - 1)

        return self._result(ok=True, stocks=stocks, skipped=skipped,
                            cache_hits=cache_hits, fetched=len(data) - cache_hits)

    # ------------------------- 数据 -------------------------

    def _resolve_scope(self) -> List[Tuple[str, str]]:
        cfg = self.cfg
        if cfg.scope == "watch":
            return self.store.get_watch()
        if cfg.scope == "codes":
            known = {c: n for c, n in self.store.get_stocks()}
            known.update({c: n for c, n in self.store.get_watch()})
            return [(c, known.get(c, c)) for c in cfg.codes if c]
        return self.store.get_stocks()

    def _estimate_limit(self) -> int:
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
        cap = PERIOD_FETCH_CAP.get(cfg.period, 5000)
        return min(max(need, 100), cap)

    def _warmup_start(self) -> str:
        cfg = self.cfg
        if not cfg.start:
            return ""
        bpd = PERIOD_BARS_PER_DAY.get(cfg.period, 8)
        try:
            d = datetime.strptime(cfg.start, "%Y-%m-%d") - timedelta(days=int(cfg.lookback / bpd) + 10)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            return ""

    def _prepare_data(self, data, names) -> Tuple[Dict[str, List[dict]], List[dict]]:
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

    # ------------------------- 撮合（单笔独立核算） -------------------------

    def _check_risk(self, bar: dict, idx: int, pos: Optional[VPosition]) -> Optional[Signal]:
        """止盈/止损/持有期；T+1：买入当日不触发风控"""
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

    def _open(self, code: str, name: str, bar: dict, sig: Signal, idx: int,
              price: Optional[float] = None) -> None:
        """出手：按固定金额买入（含滑点与手续费），不检查资金"""
        cfg = self.cfg
        base = price if price is not None else (sig.price if sig.price is not None else bar["close"])
        exec_price = base * (1 + cfg.slippage_rate)
        amount = sig.amount if sig.amount and sig.amount > 0 else cfg.amount
        if amount <= 0:
            return
        self.positions[code] = VPosition(
            code=code, name=name, buy_ts=bar["ts"], buy_price=exec_price,
            amount=amount, commission=amount * cfg.commission_rate,
            entry_bar_index=idx, buy_reason=sig.reason)

    def _close(self, code: str, name: str, bar: dict, pos: VPosition, sig: Signal,
               idx: int) -> None:
        """平仓：按信号价（-滑点）结算，单笔盈亏 = 回款 - 卖佣 - 投入 - 买佣"""
        cfg = self.cfg
        base = sig.price if sig.price is not None else bar["close"]
        exec_price = base * (1 - cfg.slippage_rate)
        shares = pos.amount / pos.buy_price
        proceeds = shares * exec_price
        sell_comm = proceeds * cfg.commission_rate
        pnl = proceeds - sell_comm - pos.amount - pos.commission
        pnl_pct = pnl / pos.amount * 100.0 if pos.amount else 0.0
        self.trades.append({
            "code": code, "name": name,
            "buy_ts": pos.buy_ts, "buy_price": round(pos.buy_price, 4),
            "sell_ts": bar["ts"], "sell_price": round(exec_price, 4),
            "amount": round(pos.amount, 2),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "hold_bars": idx - pos.entry_bar_index,
            "buy_reason": pos.buy_reason, "sell_reason": sig.reason,
        })
        del self.positions[code]

    # ------------------------- 结果 -------------------------

    def _result(self, ok: bool, msg: str = "", stocks: Optional[list] = None,
                skipped: Optional[list] = None, cache_hits: int = 0,
                fetched: int = 0) -> dict:
        cfg = self.cfg
        meta = STRATEGY_REGISTRY.get(cfg.strategy)
        trades = self.trades
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in trades)
        total_invest = sum(t["amount"] for t in trades)
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else (float("inf") if wins else 0.0)
        metrics = {
            "trade_count": len(trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2) if trades else 0.0,
            "total_pnl": round(total_pnl, 2),
            "total_invest": round(total_invest, 2),
            "total_return_pct": round(total_pnl / total_invest * 100.0, 2) if total_invest else 0.0,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio != float("inf") else None,
        }
        return {
            "ok": ok,
            "msg": msg,
            "config": asdict(cfg),
            "strategy": {"key": cfg.strategy,
                         "name": meta.name if meta else cfg.strategy,
                         "desc": meta.desc if meta else ""},
            "stocks": [{"code": c, "name": n} for c, n in (stocks or [])],
            "skipped": skipped or [],
            "warnings": self.warnings,
            "data_source": {"cache_hits": cache_hits, "fetched": fetched},
            "trades": trades,
            "metrics": metrics,
            "assumptions": _ASSUMPTIONS,
        }


async def run_validator(cfg) -> dict:
    """运行信号验证（cfg 为 ValidatorConfig 或 dict），自动释放数据源连接"""
    if isinstance(cfg, dict):
        cfg = ValidatorConfig.from_dict(cfg)
    v = Validator(cfg)
    try:
        return await v.run()
    finally:
        await v.ds.close()
