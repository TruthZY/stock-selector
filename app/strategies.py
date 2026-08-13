# -*- coding: utf-8 -*-
"""选股策略引擎：规则式多策略，实时快照类 + K线形态类"""
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import config
from app import indicators as ta
from app.bars import closed_slice

# 涨跌幅阈值：主板 10%，创业板/科创板 20%
def _limit_pct(code: str) -> float:
    return 20.0 if code.startswith(("30", "68")) else 10.0


@dataclass
class StockContext:
    """单只股票的完整判断上下文"""
    code: str
    name: str
    snap: Optional[dict] = None      # 腾讯实时快照
    daily: List[dict] = field(default_factory=list)   # 日K（升序）
    k60: List[dict] = field(default_factory=list)     # 60分钟K（升序）
    # 各周期K线 period -> 升序K线，键取自 config.REALTIME_PERIODS。
    # daily/k60 是其中两个周期的便捷别名（现有策略在用）；
    # 需要按周期取用（如把回测的 BuyRule 接进来）走这里
    bars: Dict[str, List[dict]] = field(default_factory=dict)
    # 刻意不放 params：策略参数由 StrategyEngine.evaluate 按策略 key 切片后
    # 作为第二个入参显式传给实现。这里放一份就会有人拿整个嵌套字典当扁平参数用


# ---------------------------------------------------------------------------
# 策略实现（返回 (命中, 原因)）
# ---------------------------------------------------------------------------

def _ma_bull(ctx: StockContext, params: Dict) -> tuple:
    """日K均线多头排列：MA5>MA10>MA20>MA60，且收于MA5上方（以最新已收盘日K为准）"""
    daily = closed_slice(ctx.daily)
    if len(daily) < 60:
        return False, "日K数据不足"
    closes = [k["close"] for k in daily]
    ma5, ma10 = ta.ma(closes, 5), ta.ma(closes, 10)
    ma20, ma60 = ta.ma(closes, 20), ta.ma(closes, 60)
    last = daily[-1]
    if None in (ma5[-1], ma10[-1], ma20[-1], ma60[-1]):
        return False, "均线数据不足"
    ok = ma5[-1] > ma10[-1] > ma20[-1] > ma60[-1] > 0 and last["close"] > ma5[-1]
    if ok:
        return True, (f"MA5({ma5[-1]:.2f})>MA10({ma10[-1]:.2f})>MA20({ma20[-1]:.2f})"
                      f">MA60({ma60[-1]:.2f}) @ {last['ts']}"), last["ts"]
    return False, ""


def _ma_golden_cross(ctx: StockContext, params: Dict) -> tuple:
    """日K MA5 上穿 MA10（以最新已收盘日K为准）"""
    daily = closed_slice(ctx.daily)
    if len(daily) < 12:
        return False, "日K数据不足"
    closes = [k["close"] for k in daily]
    if ta.cross_up(ta.ma(closes, 5), ta.ma(closes, 10)):
        return True, f"MA5上穿MA10 @ {daily[-1]['ts']}", daily[-1]["ts"]
    return False, ""


def _macd_golden(ctx: StockContext, params: Dict) -> tuple:
    """MACD：DIF 上穿 DEA，周期由 params["period"] 指定（默认 60m）

    此前写死取 ctx.k60，config 里那个 {"period": "60m"} 从来没被读过
    """
    period = str(params.get("period") or "60m")
    bars = closed_slice((ctx.bars or {}).get(period)
                        or (ctx.k60 if period == "60m" else []))
    if len(bars) < 35:
        return False, f"{period} K线数据不足"
    closes = [k["close"] for k in bars]
    dif, dea, _ = ta.macd(closes)
    if ta.cross_up(dif, dea):
        return True, f"{period} DIF上穿DEA @ {bars[-1]['ts']}", bars[-1]["ts"]
    return False, ""


def _volume_breakout(ctx: StockContext, params: Dict) -> tuple:
    """放量突破：量比≥阈值 且 现价突破 N 日最高（不含今日）"""
    snap = ctx.snap
    daily = ctx.daily
    if not snap or not daily:
        return False, "数据不足"
    ratio = ta.volume_ratio(snap["volume"], [k["volume"] for k in daily[:-1]])
    need = params.get("volume_ratio", 2.0)
    days = params.get("break_days", 20)
    if len(daily) < days + 1:
        return False, "日K数据不足"
    prev_highs = [k["high"] for k in daily[-(days + 1):-1]]
    top = max(prev_highs)
    if ratio >= need and snap["price"] > top > 0:
        return True, (f"量比{ratio:.1f}倍，现价{snap['price']:.2f}突破{days}日高点{top:.2f}")
    return False, ""


def _strong_up(ctx: StockContext, params: Dict) -> tuple:
    """强势拉升：涨幅≥阈值 且 非一字板（开盘价低于涨停价）"""
    snap = ctx.snap
    if not snap:
        return False, "无快照"
    pct = params.get("pct", 5.0)
    limit_up = snap.get("limit_up") or 0
    if snap["change_pct"] >= pct and limit_up and snap["open"] < limit_up:
        return True, f"涨幅{snap['change_pct']:.2f}%，现价{snap['price']:.2f}"
    return False, ""


def _limit_up(ctx: StockContext, params: Dict) -> tuple:
    """涨停预警：现价触及涨停价"""
    snap = ctx.snap
    if not snap:
        return False, "无快照"
    limit_up = snap.get("limit_up") or 0
    if limit_up > 0 and snap["price"] >= limit_up * 0.999:
        return True, f"触及涨停价{limit_up:.2f}，涨幅{snap['change_pct']:.2f}%"
    return False, ""


def _rsi_oversold(ctx: StockContext, params: Dict) -> tuple:
    """RSI超卖反弹：日K RSI14<阈值 且 当日转涨"""
    snap = ctx.snap
    daily = ctx.daily
    if not snap or len(daily) < 16:
        return False, "数据不足"
    closes = [k["close"] for k in daily]
    r = ta.rsi(closes)[-1]
    below = params.get("rsi_below", 30.0)
    if r is not None and r < below and snap["change_pct"] > 0:
        return True, f"RSI14={r:.1f}<{below} 且当日转涨{snap['change_pct']:.2f}%"
    return False, ""


def _low_pe(ctx: StockContext, params: Dict) -> tuple:
    """低估值异动：PE<阈值 且 当日涨幅≥阈值"""
    snap = ctx.snap
    if not snap:
        return False, "无快照"
    pe = snap.get("pe") or 0
    if pe <= 0 or math.isnan(pe) or pe > 1e4:  # 排除无效PE
        return False, "PE无效"
    max_pe = params.get("max_pe", 15.0)
    min_pct = params.get("min_pct", 2.0)
    if pe < max_pe and snap["change_pct"] >= min_pct:
        return True, f"PE={pe:.1f}，当日涨幅{snap['change_pct']:.2f}%"
    return False, ""


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

# 策略元信息与实现映射
STRATEGY_IMPL: Dict[str, Callable[[StockContext, Dict], tuple]] = {
    "ma_bull": _ma_bull,
    "ma_golden_cross": _ma_golden_cross,
    "macd_golden": _macd_golden,
    "volume_breakout": _volume_breakout,
    "strong_up": _strong_up,
    "limit_up": _limit_up,
    "rsi_oversold": _rsi_oversold,
    "low_pe": _low_pe,
}

# 实时类策略（仅依赖快照，随快照轮询触发）
SNAPSHOT_STRATEGIES = {"volume_breakout", "strong_up", "limit_up", "low_pe", "rsi_oversold"}
# K线类策略（依赖K线收盘后形态，随K线同步触发）
KLINE_STRATEGIES = {"ma_bull", "ma_golden_cross", "macd_golden"}


class StrategyEngine:
    """选股引擎：对单只股票执行所有启用的策略，返回命中列表"""

    def __init__(self):
        self.strategies = {
            key: dict(value) for key, value in config.DEFAULT_STRATEGIES.items()
        }
        # 按策略 key 嵌套：{策略key: {参数名: 值}}。
        # evaluate 负责把对应策略那一层切出来传给实现——曾经是把整个嵌套字典
        # 当成 ctx.params 传下去，而实现读的是扁平键，于是 config.STRATEGY_PARAMS
        # 全程无效、所有策略都跑在硬编码默认值上（改配置没反应）
        self.params = {k: dict(v) for k, v in config.STRATEGY_PARAMS.items()}
        # 用实际参数渲染 desc 里的 {xxx}，让界面说明跟着参数走
        for key, meta in self.strategies.items():
            try:
                meta["desc"] = meta["desc"].format(**self.params.get(key, {}))
            except (KeyError, IndexError, ValueError):
                pass        # 占位符与参数不匹配时保留原文，不因为文案问题崩掉引擎

    def toggle(self, key: str, enabled: bool) -> bool:
        if key in self.strategies:
            self.strategies[key]["enabled"] = enabled
            return True
        return False

    def list(self) -> List[dict]:
        return [{"key": k, **v} for k, v in self.strategies.items()]

    def evaluate(self, ctx: StockContext, kind: str = "all") -> List[dict]:
        """对一只股票执行策略，返回 [{"key","name","reason","price","change_pct"}]"""
        hits = []
        for key, meta in self.strategies.items():
            if not meta["enabled"]:
                continue
            if kind == "snapshot" and key not in SNAPSHOT_STRATEGIES:
                continue
            if kind == "kline" and key not in KLINE_STRATEGIES:
                continue
            try:
                # 只把该策略自己那一层参数传进去（扁平），实现里读 params.get("xxx")
                res = STRATEGY_IMPL[key](ctx, self.params.get(key, {}))
            except Exception:
                continue
            # 实现可返回 (命中, 原因) 或 (命中, 原因, 判定所依据的K线ts)。
            # 带 ts 的（K线类策略）由 Scanner 按"每根只报一次"去重——收盘确认后
            # 同一根已收盘K线会被反复评估，只靠时间窗去重会重复上报
            matched, reason = res[0], res[1]
            bar_ts = res[2] if len(res) > 2 else ""
            if matched:
                hits.append({
                    "key": key,
                    "name": meta["name"],
                    "reason": reason,
                    "bar_ts": bar_ts,
                    "price": (ctx.snap or {}).get("price", 0.0),
                    "change_pct": (ctx.snap or {}).get("change_pct", 0.0),
                })
        return hits
