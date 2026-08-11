# -*- coding: utf-8 -*-
"""条件组件库：买卖信号原子判断，供战法组合调用（如买入条件、卖出条件）

统一签名：fn(bars, i, **params) -> (是否命中, 原因)
- bars：升序K线列表 [{ts, open, close, high, low, volume, amount}]，任意周期（日K/30m 等由数据自身决定）
- i：当前位置索引（一般传 ctx.i，即战法当前K线）
- 返回 (bool, str)，str 用于写入交易明细 reason

战法用法示例：
    from app.backtest import conditions as cond
    hit, reason = cond.kdj_golden_cross(ctx.bars, ctx.i, n=9)
    hit2, _ = cond.time_between(ctx.bars, ctx.i, after="09:30", before="14:30")

注意：本库为无状态纯函数，高频调用（如每根K线重算 KDJ）建议在战法
prepare() 中预计算指标序列并缓存，on_bar 内只做 O(1) 判断。
"""
from typing import Optional, Tuple

from app import indicators as ta
from app import patterns as pt


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _pct(a: float, b: float) -> float:
    """涨幅百分比：(b/a - 1) * 100"""
    return (b - a) / a * 100.0 if a else 0.0


def _cross_at(a: list, b: list, i: int, up: bool = True) -> bool:
    """判断序列 a、b 在位置 i 是否发生上穿(up=True)/下穿(up=False)"""
    if i < 1:
        return False
    vals = (a[i - 1], a[i], b[i - 1], b[i])
    if any(v is None for v in vals):
        return False
    if up:
        return a[i - 1] <= b[i - 1] and a[i] > b[i]
    return a[i - 1] >= b[i - 1] and a[i] < b[i]


# ---------------------------------------------------------------------------
# 1. KDJ / RSI 金叉死叉
# ---------------------------------------------------------------------------

def kdj_golden_cross(bars: list, i: int, n: int = 9) -> Tuple[bool, str]:
    """KDJ 金叉：K 线上穿 D 线（默认 9 周期，指标实现见 app/indicators.kdj）"""
    if i < 1:
        return False, "数据不足"
    highs = [k["high"] for k in bars[:i + 1]]
    lows = [k["low"] for k in bars[:i + 1]]
    closes = [k["close"] for k in bars[:i + 1]]
    k, d, _ = ta.kdj(highs, lows, closes, n)
    if _cross_at(k, d, i, up=True):
        return True, f"KDJ金叉 K({k[i]:.1f})上穿D({d[i]:.1f}) @ {bars[i]['ts']}"
    return False, ""


def kdj_dead_cross(bars: list, i: int, n: int = 9) -> Tuple[bool, str]:
    """KDJ 死叉：K 线下穿 D 线"""
    if i < 1:
        return False, "数据不足"
    highs = [k["high"] for k in bars[:i + 1]]
    lows = [k["low"] for k in bars[:i + 1]]
    closes = [k["close"] for k in bars[:i + 1]]
    k, d, _ = ta.kdj(highs, lows, closes, n)
    if _cross_at(k, d, i, up=False):
        return True, f"KDJ死叉 K({k[i]:.1f})下穿D({d[i]:.1f}) @ {bars[i]['ts']}"
    return False, ""


def rsi_golden_cross(bars: list, i: int, fast: int = 6, slow: int = 12) -> Tuple[bool, str]:
    """RSI 金叉：快线（默认 RSI6）上穿慢线（默认 RSI12）"""
    if i < 1:
        return False, "数据不足"
    closes = [k["close"] for k in bars[:i + 1]]
    rf = ta.rsi(closes, fast)
    rs = ta.rsi(closes, slow)
    if _cross_at(rf, rs, i, up=True):
        return True, f"RSI{fast}金叉 RSI{fast}({rf[i]:.1f})上穿RSI{slow}({rs[i]:.1f}) @ {bars[i]['ts']}"
    return False, ""


def rsi_dead_cross(bars: list, i: int, fast: int = 6, slow: int = 12) -> Tuple[bool, str]:
    """RSI 死叉：快线下穿慢线"""
    if i < 1:
        return False, "数据不足"
    closes = [k["close"] for k in bars[:i + 1]]
    rf = ta.rsi(closes, fast)
    rs = ta.rsi(closes, slow)
    if _cross_at(rf, rs, i, up=False):
        return True, f"RSI{fast}死叉 RSI{fast}({rf[i]:.1f})下穿RSI{slow}({rs[i]:.1f}) @ {bars[i]['ts']}"
    return False, ""


# ---------------------------------------------------------------------------
# 2. 时间条件（分钟K）
# ---------------------------------------------------------------------------

def time_between(bars: list, i: int, after: str = "09:30", before: str = "14:30") -> Tuple[bool, str]:
    """时间窗条件：当前K线时间在 [after, before] 内（HH:MM）
    日K/周K等无时分信息的K线视为满足（时间条件仅对分钟K有意义）"""
    ts = bars[i]["ts"]
    if len(ts) <= 10:
        return True, "日K无时间限制"
    hm = ts[11:16]
    if after <= hm <= before:
        return True, f"时间{hm}在{after}~{before}内"
    return False, f"时间{hm}不在{after}~{before}内"


# ---------------------------------------------------------------------------
# 3. 趋势判断
# ---------------------------------------------------------------------------

def steady_uptrend(bars: list, i: int, lookback: int = 5, min_gain_pct: float = 0.0) -> Tuple[bool, str]:
    """一直上涨趋势：最近 lookback 根收盘价逐根抬升，且整体涨幅 >= min_gain_pct%"""
    if i + 1 < lookback + 1:
        return False, "数据不足"
    closes = [k["close"] for k in bars[i - lookback + 1:i + 1]]
    if not all(closes[t] > closes[t - 1] for t in range(1, len(closes))):
        return False, "收盘价未逐根抬升"
    gain = _pct(bars[i - lookback + 1]["close"], bars[i]["close"])
    if gain < min_gain_pct:
        return False, f"整体涨幅{gain:.1f}%不足{min_gain_pct}%"
    return True, f"连涨{lookback}根，整体涨幅{gain:.1f}%"


def uptrend_pullback(bars: list, i: int, up_lookback: int = 20, up_pct: float = 8.0,
                     pullback_lookback: int = 5, pullback_pct: float = 2.0,
                     max_pullback_pct: Optional[float] = None) -> Tuple[bool, str]:
    """上涨后回调趋势：前期 up_lookback 根内涨幅 >= up_pct%，
    最近 pullback_lookback 根从高点回落 >= pullback_pct%（可选上限 max_pullback_pct% 防止趋势破坏）"""
    need = up_lookback + pullback_lookback
    if i + 1 < need:
        return False, "数据不足"
    base = i - up_lookback               # 上涨起点
    peak = i - pullback_lookback         # 上涨终点（回调起点）
    gain = _pct(bars[base]["close"], bars[peak]["close"])
    if gain < up_pct:
        return False, f"前期涨幅{gain:.1f}%不足{up_pct}%"
    hi = max(k["high"] for k in bars[peak:i + 1])
    dd = (hi - bars[i]["close"]) / hi * 100.0 if hi else 0.0
    if dd < pullback_pct:
        return False, f"回调{dd:.1f}%不足{pullback_pct}%"
    if max_pullback_pct is not None and dd > max_pullback_pct:
        return False, f"回调{dd:.1f}%过深（>{max_pullback_pct}%）"
    return True, f"上涨后回调：前期涨{gain:.1f}%，当前回调{dd:.1f}%"


def sideways(bars: list, i: int, lookback: int = 10, range_pct: float = 5.0) -> Tuple[bool, str]:
    """横盘趋势：最近 lookback 根内 (最高-最低)/最低 <= range_pct%"""
    if i + 1 < lookback:
        return False, "数据不足"
    window = bars[i - lookback + 1:i + 1]
    lo = min(k["low"] for k in window)
    hi = max(k["high"] for k in window)
    rng = (hi - lo) / lo * 100.0 if lo else 0.0
    if rng > range_pct:
        return False, f"振幅{rng:.1f}%超过{range_pct}%"
    return True, f"横盘：{lookback}根振幅{rng:.1f}%"


def downtrend_stabilizing(bars: list, i: int, down_lookback: int = 20, down_pct: float = 8.0,
                          stabilize_lookback: int = 3) -> Tuple[bool, str]:
    """下落企稳趋势：前期 down_lookback 根内跌幅 >= down_pct%，
    最近 stabilize_lookback 根止跌企稳（低点不再创新低且末根收阳）"""
    need = down_lookback + stabilize_lookback
    if i + 1 < need:
        return False, "数据不足"
    base = i - down_lookback             # 下跌起点
    low_idx = i - stabilize_lookback     # 下跌终点（企稳起点）
    drop = _pct(bars[base]["close"], bars[low_idx]["close"])
    if drop > -down_pct:
        return False, f"前期跌幅{drop:.1f}%不足{down_pct}%"
    down_low = min(k["low"] for k in bars[base:low_idx + 1])
    recent_low = min(k["low"] for k in bars[low_idx + 1:i + 1])
    cur = bars[i]
    if recent_low < down_low:
        return False, "低点仍创新低，未企稳"
    if cur["close"] <= cur["open"]:
        return False, "末根未收阳"
    return True, f"下落企稳：前期跌{drop:.1f}%，低点抬高且收阳"


def classify_trend(bars: list, i: int, **kwargs) -> str:
    """趋势分类辅助：按默认参数（或 kwargs 覆盖）返回首个命中的趋势类型
    返回 steady_up / up_pullback / sideways / down_stabilize / other
    例：classify_trend(bars, i, up_pct=6.0) 可调整上涨后回调的涨幅阈值"""
    checks = [("steady_up", steady_uptrend), ("up_pullback", uptrend_pullback),
              ("sideways", sideways), ("down_stabilize", downtrend_stabilizing)]
    import inspect
    for name, fn in checks:
        params = {k: v for k, v in kwargs.items()
                  if k in inspect.signature(fn).parameters}
        if fn(bars, i, **params)[0]:
            return name
    return "other"


# ---------------------------------------------------------------------------
# 4. K线形态（复用 app/patterns 形态库，阳包阴/阴包阳等）
# ---------------------------------------------------------------------------

def pattern(bars: list, i: int, key: str = "yang_bao_yin", window: int = 5) -> Tuple[bool, str]:
    """最近 window 根K线内出现指定形态（key 见 app/patterns.PATTERNS，如 yang_bao_yin/yin_bao_yang）"""
    meta = pt.PATTERNS.get(key)
    if not meta:
        return False, f"未知形态 {key}"
    if i + 1 < 2:
        return False, "数据不足"
    hits = pt.scan(bars[:i + 1], key, window)
    if not hits:
        return False, f"{window}根内无{meta['name']}"
    return True, f"{meta['name']} @ {hits[-1]['ts']}"


def bullish_engulfing(bars: list, i: int, window: int = 5) -> Tuple[bool, str]:
    """阳包阴：前一根阴线被当前（或窗口内）阳线实体完全包住，短线转强"""
    return pattern(bars, i, "yang_bao_yin", window)


def pattern_counts(bars: list, i: int, days: int = 2) -> dict:
    """统计最近 days 个交易日（含当前K线所在日）内各形态出现次数
    返回 {形态key: 次数}，key 覆盖 app/patterns.PATTERNS 全部形态
    例：买入要求阳包阴次数 > 阴包阳次数
        counts = pattern_counts(bars, i, days=2)
        ok = counts.get("yang_bao_yin", 0) > counts.get("yin_bao_yang", 0)"""
    if i < 1:
        return {key: 0 for key in pt.PATTERNS}
    # 从当前位置向前收集最近 days 个交易日
    dates, seen = [], set()
    for j in range(i, -1, -1):
        d = bars[j]["ts"][:10]
        if d not in seen:
            seen.add(d)
            dates.append(d)
        if len(dates) >= days:
            break
    window = [k for k in bars[:i + 1] if k["ts"][:10] in seen]
    out = {}
    for key in pt.PATTERNS:
        out[key] = len(pt.scan(window, key, max(len(window), 2)))
    return out


def bearish_engulfing(bars: list, i: int, window: int = 5) -> Tuple[bool, str]:
    """阴包阳：前一根阳线被当前（或窗口内）阴线实体完全包住，短线转弱"""
    return pattern(bars, i, "yin_bao_yang", window)
