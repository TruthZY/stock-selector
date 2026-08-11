# -*- coding: utf-8 -*-
"""K线形态库：面向短线交易的自定义形态筛选
当前支持：阳包阴、阴包阳（可在 PATTERNS 中继续扩展新形态）
"""
from typing import List


def yang_bao_yin(prev: dict, cur: dict) -> bool:
    """阳包阴：前一根阴线，当前阳线，且阳线实体完全包住前一根阴线实体"""
    if prev["close"] >= prev["open"] or cur["close"] <= cur["open"]:
        return False
    return cur["close"] >= prev["open"] and cur["open"] <= prev["close"]


def yin_bao_yang(prev: dict, cur: dict) -> bool:
    """阴包阳：前一根阳线，当前阴线，且阴线实体完全包住前一根阳线实体"""
    if prev["close"] <= prev["open"] or cur["close"] >= cur["open"]:
        return False
    return cur["open"] >= prev["close"] and cur["close"] <= prev["open"]


PATTERNS = {
    "yang_bao_yin": {"name": "阳包阴", "desc": "阳线实体包住前一根阴线实体，短线转强信号", "fn": yang_bao_yin},
    "yin_bao_yang": {"name": "阴包阳", "desc": "阴线实体包住前一根阳线实体，短线转弱信号", "fn": yin_bao_yang},
}


def scan(klines: List[dict], pattern_key: str, window: int = 5) -> List[dict]:
    """在 klines（按时间升序）的最后 window 根K线内扫描形态
    返回命中列表 [{ts, prev_ts}]，ts 为触发形态的那根K线时间"""
    fn = PATTERNS.get(pattern_key, {}).get("fn")
    if not fn or not klines:
        return []
    bars = klines[-max(window, 2):]
    hits = []
    for i in range(1, len(bars)):
        if fn(bars[i - 1], bars[i]):
            hits.append({"ts": bars[i]["ts"], "prev_ts": bars[i - 1]["ts"]})
    return hits


def trend_before(klines: List[dict], ts: str, lookback: int = 5,
                 threshold: float = 1.5) -> tuple:
    """形态最早触发K线之前 lookback 根的趋势（可跨日）。
    用收盘价累计涨跌幅判定：>=+threshold% 上升，<=-threshold% 下降，其余平稳。
    返回 (trend, pct)，trend ∈ up/down/flat"""
    idx = next((i for i, k in enumerate(klines) if k["ts"] >= ts), len(klines))
    prev = klines[max(0, idx - lookback):idx]
    if len(prev) < 2 or not prev[0].get("close"):
        return "flat", 0.0
    pct = (prev[-1]["close"] - prev[0]["close"]) / prev[0]["close"] * 100
    if pct >= threshold:
        return "up", round(pct, 2)
    if pct <= -threshold:
        return "down", round(pct, 2)
    return "flat", round(pct, 2)
