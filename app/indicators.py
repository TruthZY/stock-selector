# -*- coding: utf-8 -*-
"""技术指标计算（纯 Python，无第三方依赖）"""
from typing import List, Optional, Tuple


def ma(values: List[float], n: int) -> List[Optional[float]]:
    """简单移动平均，前 n-1 项为 None"""
    out: List[Optional[float]] = [None] * len(values)
    if not values:
        return out
    s = sum(values[:n])
    out[n - 1] = s / n
    for i in range(n, len(values)):
        s += values[i] - values[i - n]
        out[i] = s / n
    return out


def ema(values: List[float], n: int) -> List[float]:
    """指数移动平均（首项取第一个值）"""
    out: List[float] = []
    if not values:
        return out
    k = 2.0 / (n + 1)
    prev = values[0]
    out.append(prev)
    for v in values[1:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (dif, dea, hist) 三个等长序列"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [a - b for a, b in zip(ema_fast, ema_slow)]
    dea = ema(dif, signal)
    hist = [2 * (d - s) for d, s in zip(dif, dea)]
    return dif, dea, hist


def rsi(closes: List[float], n: int = 14) -> List[Optional[float]]:
    """RSI（Wilder 平滑）"""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def kdj(highs: List[float], lows: List[float], closes: List[float], n: int = 9):
    """KDJ，返回 (k, d, j) 三元组序列"""
    k, d = 50.0, 50.0
    ks, ds, js = [], [], []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - n + 1):i + 1])
        hi = max(highs[max(0, i - n + 1):i + 1])
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100.0
        k = 2.0 / 3.0 * k + 1.0 / 3.0 * rsv
        d = 2.0 / 3.0 * d + 1.0 / 3.0 * k
        ks.append(k)
        ds.append(d)
        js.append(3 * k - 2 * d)
    return ks, ds, js


def boll(closes: List[float], n: int = 20, k: float = 2.0):
    """布林带，返回 (mid, upper, lower)"""
    mid = ma(closes, n)
    upper: List[Optional[float]] = [None] * len(closes)
    lower: List[Optional[float]] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1:i + 1]
        mean = mid[i]
        var = sum((x - mean) ** 2 for x in window) / n
        sd = var ** 0.5
        upper[i] = mean + k * sd
        lower[i] = mean - k * sd
    return mid, upper, lower


def cross_up(a: List[Optional[float]], b: List[Optional[float]]) -> bool:
    """a 上穿 b（判断序列最后一个点）"""
    if len(a) < 2 or len(b) < 2:
        return False
    p1, p2 = a[-2], b[-2]
    c1, c2 = a[-1], b[-1]
    if None in (p1, p2, c1, c2):
        return False
    return p1 <= p2 and c1 > c2


def volume_ratio(cur_volume: float, daily_volumes: List[float]) -> float:
    """量比：当日已成交量 / 前5日日均成交量（简化为前5日平均量）"""
    if not daily_volumes:
        return 0.0
    base = sum(daily_volumes[-5:]) / len(daily_volumes[-5:])
    if base <= 0:
        return 0.0
    return cur_volume / base
