# -*- coding: utf-8 -*-
"""战法规则组件库：买入规则 / 卖出规则 独立注册、自由组合

设计：
- BuyRule：只产生买入信号的规则（无持仓时调用）
- SellRule：只产生卖出信号的规则（持仓中调用），可持状态（如吊灯止损峰值）
- 组合战法 ComboStrategy：任意 买入规则 + 卖出规则 组装成一个完整战法，
  引擎/验证器按 config.buy_rule + config.sell_rule 构建
- 每个规则声明 default_params（默认参数）与 PARAM_LABELS（参数中文名），
  前端验证台据此渲染可编辑参数表单

新增规则：继承 BuyRule/SellRule + @register_buy(key)/@register_sell(key)
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Type

from app import indicators as ta
from app import patterns as pt
from app.backtest import conditions as cond
from app.backtest.position import Position
from app.backtest.strategy import (BarContext, BaseStrategy, Signal,
                                   get_strategy)

# ---------------------------------------------------------------------------
# 规则基类与注册机制
# ---------------------------------------------------------------------------


class BuyRule(ABC):
    """买入规则基类：on_bar 返回 buy 信号或 None（由组合层保证无持仓时调用）"""

    key: str = ""
    name: str = ""
    desc: str = ""
    default_params: Dict = {}
    PARAM_LABELS: Dict[str, str] = {}

    def __init__(self):
        self.params: Dict = dict(self.default_params or {})

    def reset(self) -> None:
        self.params = dict(self.default_params or {})

    def prepare(self, bars: List[dict]) -> None:
        """可选：预计算指标序列"""

    def on_position_opened(self, ctx: BarContext, position: Position) -> None:
        """可选：买入成交后调用"""

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        """当前无持仓时调用，返回 buy 信号或 None"""


class SellRule(ABC):
    """卖出规则基类：on_bar 返回 sell 信号或 None（由组合层保证持仓中调用）"""

    key: str = ""
    name: str = ""
    desc: str = ""
    default_params: Dict = {}
    PARAM_LABELS: Dict[str, str] = {}

    def __init__(self):
        self.params: Dict = dict(self.default_params or {})

    def reset(self) -> None:
        self.params = dict(self.default_params or {})

    def prepare(self, bars: List[dict]) -> None:
        """可选：预计算指标序列"""

    def on_position_opened(self, ctx: BarContext, position: Position) -> None:
        """可选：买入成交后调用（重置吊灯止损峰值等）"""

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        """持仓中调用，返回 sell 信号或 None"""


BUY_REGISTRY: Dict[str, Type[BuyRule]] = {}
SELL_REGISTRY: Dict[str, Type[SellRule]] = {}


def register_buy(key: str):
    def deco(cls: Type[BuyRule]) -> Type[BuyRule]:
        cls.key = key
        BUY_REGISTRY[key] = cls
        return cls
    return deco


def register_sell(key: str):
    def deco(cls: Type[SellRule]) -> Type[SellRule]:
        cls.key = key
        SELL_REGISTRY[key] = cls
        return cls
    return deco


def list_rules() -> dict:
    """列出全部买入/卖出规则（含默认参数与参数中文名），供前端渲染"""
    def fmt(reg: Dict[str, Type]) -> List[dict]:
        return [{"key": cls.key, "name": cls.name, "desc": cls.desc,
                 "default_params": dict(cls.default_params or {}),
                 "param_labels": dict(getattr(cls, "PARAM_LABELS", {}))}
                for cls in reg.values()]
    return {"buy_rules": fmt(BUY_REGISTRY), "sell_rules": fmt(SELL_REGISTRY)}


# ---------------------------------------------------------------------------
# 买入规则
# ---------------------------------------------------------------------------


@register_buy("kdj_rsi_golden")
class KdjRsiGoldenBuy(BuyRule):
    """KDJ+RSI 双金叉共振买入（低位/量能/涨幅/趋势/形态过滤）"""
    key = "kdj_rsi_golden"
    name = "KDJ+RSI双金叉共振"
    desc = "近N根内KDJ与RSI双金叉，11点后，趋势上涨回调/横盘/下落企稳，阳包阴>阴包阳；低位/量能/涨幅过滤"
    default_params = {
        "buy_amount": 10000.0,      # 每笔固定买入金额（元）
        "kdj_n": 9,                 # KDJ 周期
        "rsi_fast": 12,             # RSI 快线周期
        "rsi_slow": 24,             # RSI 慢线周期
        "golden_window": 3,         # 双金叉判定窗口（根）
        "after": "11:00",           # 最早买入时间 HH:MM
        "pattern_days": 2,          # 阳包阴/阴包阳统计天数
        "max_k": 55.0,              # 金叉时K值上限（低位过滤，0=关闭）
        "max_rsi": 55.0,            # 金叉时RSI快线上限（0=关闭）
        "volume_ratio": 1.3,        # 量能倍数：窗口最大量/前20根均量（0=关闭）
        "max_gain_pct": 6.0,        # 窗口累计涨幅上限%（0=关闭）
    }
    PARAM_LABELS = {
        "buy_amount": "每笔买入金额(元)", "kdj_n": "KDJ周期",
        "rsi_fast": "RSI快线周期", "rsi_slow": "RSI慢线周期",
        "golden_window": "双金叉窗口(根)", "after": "最早买入时间",
        "pattern_days": "形态统计天数", "max_k": "金叉K值上限(0关)",
        "max_rsi": "金叉RSI上限(0关)", "volume_ratio": "量能倍数(0关)",
        "max_gain_pct": "窗口涨幅上限%(0关)",
    }

    def reset(self) -> None:
        super().reset()
        self._k = self._d = []
        self._rf = self._rs = []
        self._bull = self._bear = []
        self._dates = []

    def prepare(self, bars: list) -> None:
        highs = [k["high"] for k in bars]
        lows = [k["low"] for k in bars]
        closes = [k["close"] for k in bars]
        self._k, self._d, _ = ta.kdj(highs, lows, closes, self.params["kdj_n"])
        self._rf = ta.rsi(closes, self.params["rsi_fast"])
        self._rs = ta.rsi(closes, self.params["rsi_slow"])
        self._bull = [False] * len(bars)
        self._bear = [False] * len(bars)
        for j in range(1, len(bars)):
            self._bull[j] = pt.yang_bao_yin(bars[j - 1], bars[j])
            self._bear[j] = pt.yin_bao_yang(bars[j - 1], bars[j])
        self._dates = [k["ts"][:10] for k in bars]

    @staticmethod
    def _cross(a: list, b: list, j: int, up: bool) -> bool:
        if j < 1:
            return False
        if None in (a[j - 1], a[j], b[j - 1], b[j]):
            return False
        if up:
            return a[j - 1] <= b[j - 1] and a[j] > b[j]
        return a[j - 1] >= b[j - 1] and a[j] < b[j]

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        bar = ctx.bar()
        reasons = []
        w0 = max(0, ctx.i - self.params["golden_window"] + 1)
        window = range(w0, ctx.i + 1)

        # 1. 近 golden_window 根内 KDJ 与 RSI 双金叉均出现过
        if not (any(self._cross(self._k, self._d, j, up=True) for j in window)
                and any(self._cross(self._rf, self._rs, j, up=True) for j in window)):
            return None
        reasons.append(f"近{self.params['golden_window']}根双金叉")

        # 2. 低位过滤（0=关闭）
        max_k = float(self.params.get("max_k") or 0)
        max_rsi = float(self.params.get("max_rsi") or 0)
        if max_k > 0 and not any(
                self._cross(self._k, self._d, j, up=True) and self._k[j] <= max_k
                for j in window):
            return None
        if max_rsi > 0 and not any(
                self._cross(self._rf, self._rs, j, up=True) and self._rf[j] <= max_rsi
                for j in window):
            return None
        if max_k > 0 or max_rsi > 0:
            reasons.append("低位金叉")

        # 3. 量能确认（0=关闭）
        vr = float(self.params.get("volume_ratio") or 0)
        if vr > 0 and w0 >= 1:
            win_vol = max(k["volume"] for k in ctx.bars[w0:ctx.i + 1])
            base = ctx.bars[max(0, w0 - 20):w0]
            base_vol = sum(k["volume"] for k in base) / len(base) if base else 0.0
            if base_vol > 0 and win_vol / base_vol < vr:
                return None
            reasons.append(f"量比{win_vol / base_vol:.1f}")

        # 4. 涨幅过滤（0=关闭）
        max_gain = float(self.params.get("max_gain_pct") or 0)
        if max_gain > 0 and w0 >= 1:
            gain = (bar["close"] - ctx.bars[w0 - 1]["close"]) / ctx.bars[w0 - 1]["close"] * 100.0
            if gain > max_gain:
                return None
            reasons.append(f"窗口涨{gain:.1f}%")

        # 5. 时间条件
        hit, r = cond.time_between(ctx.bars, ctx.i, after=str(self.params["after"]))
        if not hit:
            return None
        reasons.append(r)

        # 6. 趋势：上涨回调/横盘/下落企稳
        trend = cond.classify_trend(ctx.bars, ctx.i)
        if trend not in ("up_pullback", "sideways", "down_stabilize"):
            return None
        reasons.append(f"趋势:{trend}")

        # 7. 形态：近 pattern_days 个交易日内阳包阴 > 阴包阳
        day_set = set()
        for j in range(ctx.i, -1, -1):
            day_set.add(self._dates[j])
            if len(day_set) >= self.params["pattern_days"]:
                break
        bull = bear = 0
        for j in range(ctx.i, -1, -1):
            if self._dates[j] in day_set:
                bull += int(self._bull[j])
                bear += int(self._bear[j])
        if bull <= 0 or bull <= bear:
            return None
        reasons.append(f"阳包阴{bull}次>阴包阳{bear}次")

        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason="；".join(reasons))


@register_buy("macd_golden")
class MacdGoldenBuy(BuyRule):
    """MACD 金叉买入"""
    key = "macd_golden"
    name = "MACD金叉"
    desc = "DIF 上穿 DEA（金叉）即买入"
    default_params = {"buy_amount": 10000.0, "fast": 12, "slow": 26, "signal": 9}
    PARAM_LABELS = {"buy_amount": "每笔买入金额(元)",
                    "fast": "MACD快线周期", "slow": "MACD慢线周期",
                    "signal": "MACD信号周期"}

    def reset(self) -> None:
        super().reset()
        self._dif = self._dea = []

    def prepare(self, bars: list) -> None:
        closes = [k["close"] for k in bars]
        self._dif, self._dea, _ = ta.macd(
            closes, self.params["fast"], self.params["slow"], self.params["signal"])

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        if ctx.i < 1:
            return None
        d0, e0 = self._dif[ctx.i - 1], self._dea[ctx.i - 1]
        d1, e1 = self._dif[ctx.i], self._dea[ctx.i]
        if None in (d0, e0, d1, e1):
            return None
        if d0 <= e0 and d1 > e1:
            return Signal("buy", amount=float(self.params["buy_amount"]),
                          reason=f"MACD金叉 @ {ctx.bar()['ts']}")
        return None


@register_buy("kdj_golden")
class KdjGoldenBuy(BuyRule):
    """KDJ 金叉买入（低位过滤）"""
    key = "kdj_golden"
    name = "KDJ金叉"
    desc = "KDJ 金叉买入，可加时间与低位过滤"
    default_params = {"buy_amount": 10000.0, "n": 9, "after": "09:30", "max_k": 0.0}
    PARAM_LABELS = {"buy_amount": "每笔买入金额(元)", "n": "KDJ周期",
                    "after": "最早买入时间", "max_k": "金叉K值上限(0关)"}

    def reset(self) -> None:
        super().reset()
        self._k = self._d = []

    def prepare(self, bars: list) -> None:
        self._k, self._d, _ = ta.kdj(
            [k["high"] for k in bars], [k["low"] for k in bars],
            [k["close"] for k in bars], self.params["n"])

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        if ctx.i < 1:
            return None
        if None in (self._k[ctx.i - 1], self._k[ctx.i],
                    self._d[ctx.i - 1], self._d[ctx.i]):
            return None
        if not (self._k[ctx.i - 1] <= self._d[ctx.i - 1] and self._k[ctx.i] > self._d[ctx.i]):
            return None
        max_k = float(self.params.get("max_k") or 0)
        if max_k > 0 and self._k[ctx.i] > max_k:
            return None
        hit, _ = cond.time_between(ctx.bars, ctx.i, after=str(self.params["after"]))
        if not hit:
            return None
        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason=f"KDJ金叉 @ {ctx.bar()['ts']}")


# ---------------------------------------------------------------------------
# 卖出规则
# ---------------------------------------------------------------------------


@register_sell("trailing_death_cross")
class TrailingDeathCrossSell(SellRule):
    """吊灯止损 + 死叉确认卖出（双金叉战法 v2 默认卖出）"""
    key = "trailing_death_cross"
    name = "吊灯止损+死叉确认"
    desc = "固定止损→吊灯止损→KDJ死叉(需确认)→RSI死叉(需确认)；死叉确认=收盘跌破MA10或高点回撤"
    default_params = {
        "stop_loss_pct": 8.0,           # 固定止损%
        "trailing_stop_pct": 3.0,       # 吊灯止损：最高价回撤%（0=关闭）
        "dead_cross_confirm": "any",    # KDJ死叉确认: none/ma10/peak/any
        "dead_confirm_pct": 3.0,        # 高点回撤确认阈值%
        "rsi_dead_cross": "strict",     # RSI死叉: on/strict(跌破MA5)/off
    }
    PARAM_LABELS = {
        "stop_loss_pct": "固定止损%", "trailing_stop_pct": "吊灯回撤%(0关)",
        "dead_cross_confirm": "死叉确认方式", "dead_confirm_pct": "回撤确认阈值%",
        "rsi_dead_cross": "RSI死叉规则",
    }

    def reset(self) -> None:
        super().reset()
        self._k = self._d = []
        self._rf = self._rs = []
        self._ma5 = self._ma10 = []
        self._peaks: Dict[str, float] = {}   # buy_ts -> 持仓期间最高价（多笔独立）

    def prepare(self, bars: list) -> None:
        highs = [k["high"] for k in bars]
        lows = [k["low"] for k in bars]
        closes = [k["close"] for k in bars]
        self._k, self._d, _ = ta.kdj(highs, lows, closes, self.params.get("kdj_n", 9))
        self._rf = ta.rsi(closes, int(self.params.get("rsi_fast", 12)))
        self._rs = ta.rsi(closes, int(self.params.get("rsi_slow", 24)))
        self._ma5 = ta.ma(closes, 5)
        self._ma10 = ta.ma(closes, 10)

    def on_position_opened(self, ctx: BarContext, position: Position) -> None:
        self._peaks[position.buy_ts] = position.buy_price

    def on_position_closed(self, ctx: BarContext, position: Position) -> None:
        self._peaks.pop(position.buy_ts, None)

    @staticmethod
    def _cross(a: list, b: list, j: int, up: bool) -> bool:
        if j < 1:
            return False
        if None in (a[j - 1], a[j], b[j - 1], b[j]):
            return False
        if up:
            return a[j - 1] <= b[j - 1] and a[j] > b[j]
        return a[j - 1] >= b[j - 1] and a[j] < b[j]

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        pos: Position = ctx.position
        bar = ctx.bar()
        peak = self._peaks.get(pos.buy_ts, pos.buy_price)
        peak = max(peak, bar["high"])
        self._peaks[pos.buy_ts] = peak
        sl_pct = float(self.params.get("stop_loss_pct") or 0)
        trail_pct = float(self.params.get("trailing_stop_pct") or 0)

        # 1. 固定止损
        if sl_pct > 0:
            sl = pos.buy_price * (1 - sl_pct / 100.0)
            if bar["low"] <= sl:
                return Signal("sell", price=sl, reason=f"跌破{sl_pct}%止损")

        # 2. 吊灯止损
        if trail_pct > 0 and peak > 0:
            ts = peak * (1 - trail_pct / 100.0)
            if bar["low"] <= ts:
                return Signal("sell", price=ts, reason=f"高位回撤{trail_pct}%吊灯止损")

        # 3. KDJ 死叉（确认）
        if self._cross(self._k, self._d, ctx.i, up=False) and self._dead_confirmed(bar, ctx.i, peak):
            return Signal("sell", reason=f"KDJ死叉确认 @ {bar['ts']}")

        # 4. RSI 死叉（规则）
        if self._cross(self._rf, self._rs, ctx.i, up=False) and self._rsi_rule(bar, ctx.i):
            return Signal("sell", reason=f"RSI死叉确认 @ {bar['ts']}")
        return None

    def _dead_confirmed(self, bar: dict, i: int, peak: float) -> bool:
        mode = str(self.params.get("dead_cross_confirm") or "any")
        if mode == "none":
            return True
        c = bar["close"]
        ma10 = self._ma10[i] if i < len(self._ma10) else None
        hit_ma = ma10 is not None and c < ma10
        confirm_pct = float(self.params.get("dead_confirm_pct") or 3.0)
        hit_peak = peak > 0 and (peak - c) / peak * 100.0 >= confirm_pct
        if mode == "ma10":
            return hit_ma
        if mode == "peak":
            return hit_peak
        return hit_ma or hit_peak

    def _rsi_rule(self, bar: dict, i: int) -> bool:
        mode = str(self.params.get("rsi_dead_cross") or "strict")
        if mode == "off":
            return False
        if mode == "on":
            return True
        ma5 = self._ma5[i] if i < len(self._ma5) else None
        return ma5 is not None and bar["close"] < ma5


@register_sell("death_cross")
class DeathCrossSell(SellRule):
    """死叉直接卖出（v1 风格，敏感快速）"""
    key = "death_cross"
    name = "死叉直接卖出"
    desc = "KDJ 死叉即卖（可加 RSI 死叉），固定止损兜底"
    default_params = {"stop_loss_pct": 8.0, "kdj_n": 9,
                      "rsi_dead_cross": "off"}
    PARAM_LABELS = {"stop_loss_pct": "固定止损%", "kdj_n": "KDJ周期",
                    "rsi_dead_cross": "RSI死叉规则(on/off)"}

    def reset(self) -> None:
        super().reset()
        self._k = self._d = []
        self._rf = self._rs = []

    def prepare(self, bars: list) -> None:
        highs = [k["high"] for k in bars]
        lows = [k["low"] for k in bars]
        closes = [k["close"] for k in bars]
        self._k, self._d, _ = ta.kdj(highs, lows, closes, self.params["kdj_n"])
        self._rf = ta.rsi(closes, 12)
        self._rs = ta.rsi(closes, 24)

    @staticmethod
    def _cross(a: list, b: list, j: int, up: bool) -> bool:
        if j < 1:
            return False
        if None in (a[j - 1], a[j], b[j - 1], b[j]):
            return False
        if up:
            return a[j - 1] <= b[j - 1] and a[j] > b[j]
        return a[j - 1] >= b[j - 1] and a[j] < b[j]

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        pos: Position = ctx.position
        bar = ctx.bar()
        sl_pct = float(self.params.get("stop_loss_pct") or 0)
        if sl_pct > 0:
            sl = pos.buy_price * (1 - sl_pct / 100.0)
            if bar["low"] <= sl:
                return Signal("sell", price=sl, reason=f"跌破{sl_pct}%止损")
        if self._cross(self._k, self._d, ctx.i, up=False):
            return Signal("sell", reason=f"KDJ死叉 @ {bar['ts']}")
        if self.params.get("rsi_dead_cross") == "on" \
                and self._cross(self._rf, self._rs, ctx.i, up=False):
            return Signal("sell", reason=f"RSI死叉 @ {bar['ts']}")
        return None


@register_sell("macd_death")
class MacdDeathSell(SellRule):
    """MACD 死叉卖出"""
    key = "macd_death"
    name = "MACD死叉"
    desc = "DIF 下穿 DEA（死叉）即卖出，固定止损兜底"
    default_params = {"stop_loss_pct": 8.0, "fast": 12, "slow": 26, "signal": 9}
    PARAM_LABELS = {"stop_loss_pct": "固定止损%",
                    "fast": "MACD快线周期", "slow": "MACD慢线周期",
                    "signal": "MACD信号周期"}

    def reset(self) -> None:
        super().reset()
        self._dif = self._dea = []

    def prepare(self, bars: list) -> None:
        closes = [k["close"] for k in bars]
        self._dif, self._dea, _ = ta.macd(
            closes, self.params["fast"], self.params["slow"], self.params["signal"])

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        pos: Position = ctx.position
        bar = ctx.bar()
        sl_pct = float(self.params.get("stop_loss_pct") or 0)
        if sl_pct > 0:
            sl = pos.buy_price * (1 - sl_pct / 100.0)
            if bar["low"] <= sl:
                return Signal("sell", price=sl, reason=f"跌破{sl_pct}%止损")
        if ctx.i >= 1:
            d0, e0 = self._dif[ctx.i - 1], self._dea[ctx.i - 1]
            d1, e1 = self._dif[ctx.i], self._dea[ctx.i]
            if None not in (d0, e0, d1, e1) and d0 >= e0 and d1 < e1:
                return Signal("sell", reason=f"MACD死叉 @ {bar['ts']}")
        return None


# ---------------------------------------------------------------------------
# 组合战法
# ---------------------------------------------------------------------------


class ComboStrategy(BaseStrategy):
    """组合战法：买入规则 + 卖出规则 组装，参数合并（同名字段共用）"""

    key = "combo"
    name = "组合战法"
    desc = "买入规则 + 卖出规则 自由组合"

    def __init__(self, buy_key: str, sell_key: str):
        if buy_key not in BUY_REGISTRY:
            raise KeyError(f"未知买入规则: {buy_key}")
        if sell_key not in SELL_REGISTRY:
            raise KeyError(f"未知卖出规则: {sell_key}")
        super().__init__()
        self.buy_key = buy_key
        self.sell_key = sell_key
        self.buy: BuyRule = BUY_REGISTRY[buy_key]()
        self.sell: SellRule = SELL_REGISTRY[sell_key]()
        self.default_params = {**self.buy.default_params, **self.sell.default_params}
        self.params = dict(self.default_params)
        self.name = f"{self.buy.name} + {self.sell.name}"   # 组合名（子类可覆盖）

    def reset(self) -> None:
        super().reset()
        self.buy.reset()
        self.sell.reset()

    def prepare(self, bars: list) -> None:
        # 组合参数注入组件（buy/sell 各自读取自己的键）
        self.buy.params = self.params
        self.sell.params = self.params
        self.buy.prepare(bars)
        self.sell.prepare(bars)

    def on_position_opened(self, ctx: BarContext, position: Position) -> None:
        self.sell.on_position_opened(ctx, position)
        self.buy.on_position_opened(ctx, position)

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        if ctx.position is not None:
            sig = self.sell.on_bar(ctx)
            return sig if sig is not None and sig.action == "sell" else None
        sig = self.buy.on_bar(ctx)
        return sig if sig is not None and sig.action == "buy" else None


def build_strategy(cfg) -> BaseStrategy:
    """按配置构建策略实例：优先 买入规则+卖出规则 组合；否则按整包战法 key。
    cfg 需含 buy_rule/sell_rule/strategy/params 字段（engine/validator 配置类）
    """
    buy_key = getattr(cfg, "buy_rule", "") or ""
    sell_key = getattr(cfg, "sell_rule", "") or ""
    if buy_key and sell_key:
        st = ComboStrategy(buy_key, sell_key)
    else:
        st = get_strategy(getattr(cfg, "strategy", "") or "all_in_all_out")
    st.reset()
    st.params = {**st.default_params, **(getattr(cfg, "params", None) or {})}
    return st
