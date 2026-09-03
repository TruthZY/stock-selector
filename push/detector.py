# -*- coding: utf-8 -*-
"""信号检测：把回测买入战法（BuyRule）复用到"最新一根"上，判断今日是否触发。

严格遵循既有契约（顺序不可乱，见 docs 与 app/backtest/rules.py）：
    rule = cls(); rule.reset(); rule.params = {**cls.default_params, **overrides};
    rule.prepare(bars); sig = rule.on_bar(BarContext(code,name,bars,i,None,rule.params))
每股一个独立实例（跨股共用会串指标 / IndexError）。单股异常视为未命中，不打断整场
（与 app/rule_scan._eval_on_day 的容错一致）。

只读 import：app.backtest.rules / app.backtest.strategy / app.bars。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# accumulation 的 position_lookback=200，低于此窗口条件会退化；给个硬下限
DEFAULT_MIN_BARS = 60

# 从买入战法 reason 文本里解析"量比X.X"
_VOL_RE = re.compile(r"量比\s*([0-9]+(?:\.[0-9]+)?)")


def _parse_strength(reason: str) -> tuple:
    """从 reason 文本解析 (命中条件数, 量比)。

    不改 rules.py，只在推送层防御性解析其输出文本，格式形如：
        "建仓信号：BOLL下轨上移，低点抬高(34.40→35.09)，低位30%，量比1.2"
    条件数 = "：" 后按中英文逗号切分的非空段数；量比 = 正则抽取，缺省 0.0。
    解析失败时返回 (0, 0.0)，不影响主流程。
    """
    if not reason:
        return 0, 0.0
    body = reason.split("：", 1)[1] if "：" in reason else reason
    parts = [p for p in re.split(r"[，,]", body) if p.strip()]
    n_cond = len(parts)
    m = _VOL_RE.search(reason)
    vol = float(m.group(1)) if m else 0.0
    return n_cond, vol


def ensure_rules_loaded() -> None:
    """让内置 + user_rules/ 自定义买入规则都进 BUY_REGISTRY（幂等）。"""
    try:
        from app.backtest.rules import _ensure_user_rules
        _ensure_user_rules()
    except Exception:
        pass


def get_rule_class(key: str):
    ensure_rules_loaded()
    from app.backtest.rules import BUY_REGISTRY
    return BUY_REGISTRY.get(key)


def list_rule_keys() -> List[str]:
    ensure_rules_loaded()
    from app.backtest.rules import BUY_REGISTRY
    return sorted(BUY_REGISTRY)


def _resolve_index(bars: List[dict], mode: str) -> Optional[int]:
    """live=判定末根（含进行中，盘中语义）；close=判定最后一根已收盘K线。"""
    if not bars:
        return None
    if mode == "close":
        try:
            from app.bars import last_closed_index
            return last_closed_index(bars)
        except Exception:
            return len(bars) - 1
    return len(bars) - 1


def detect_one(code: str, name: str, bars: Optional[List[dict]], rule_cls,
               params: Optional[Dict] = None, mode: str = "live",
               min_bars: int = DEFAULT_MIN_BARS) -> Optional[dict]:
    """对单只股票跑一次买入战法，命中返回 hit dict，否则 None。

    hit: {code, name, reason, ts, price(close of判定bar), i}
    """
    if not bars or len(bars) < min_bars:
        return None
    i = _resolve_index(bars, mode)
    if i is None:
        return None
    try:
        from app.backtest.strategy import BarContext
        rule = rule_cls()
        rule.reset()                                   # 1. 先 reset
        rule.params = {**(getattr(rule_cls, "default_params", {}) or {}),
                       **(params or {})}               # 2. 再注入参数
        rule.prepare(bars)                             # 3. 用整段历史 prepare
        sig = rule.on_bar(BarContext(code, name, bars, i, None, rule.params))
    except Exception:
        return None                                    # 单股异常=未命中，不阻断
    if sig is None or getattr(sig, "action", "") != "buy":
        return None
    bar = bars[i]
    reason = getattr(sig, "reason", "") or ""
    n_cond, vol_ratio = _parse_strength(reason)
    return {"code": code, "name": name or code,
            "reason": reason, "ts": bar.get("ts", ""),
            "price": bar.get("close"), "i": i,
            "n_conditions": n_cond, "vol_ratio": vol_ratio}


def detect_many(items: List[dict], rule_cls, params: Optional[Dict] = None,
                mode: str = "live", min_bars: int = DEFAULT_MIN_BARS) -> List[dict]:
    """对多只股票批量检测。items: [{code,name,bars}]，返回命中清单。"""
    hits: List[dict] = []
    for it in items:
        h = detect_one(it.get("code", ""), it.get("name", ""), it.get("bars"),
                       rule_cls, params=params, mode=mode, min_bars=min_bars)
        if h is not None:
            hits.append(h)
    return hits
