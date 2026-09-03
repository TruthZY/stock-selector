# -*- coding: utf-8 -*-
"""战法规则组件库：买入规则 / 卖出规则 独立注册、自由组合

设计：
- BuyRule：只产生买入信号的规则（无持仓时调用）
- SellRule：只产生卖出信号的规则（持仓中调用），可持状态（如吊灯止损峰值）
- 组合战法 ComboStrategy：任意 买入规则 + 卖出规则 组装成一个完整战法，
  引擎/验证器按 config.buy_rule + config.sell_rule 构建
- 每个规则声明 default_params（默认参数）与 PARAM_LABELS（参数中文名），
  前端验证台据此渲染可编辑参数表单
- 可选声明 PARAM_META：告诉前端该参数的控件形态（枚举单选/多选/时间），
  否则前端只能按默认值的类型猜（数字→number，其余→自由文本），
  枚举值要靠手输、拼错了也没提示

新增规则：继承 BuyRule/SellRule + @register_buy(key)/@register_sell(key)
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Type

from app import indicators as ta
from app import patterns as pt
from app.backtest import conditions as cond
from app.backtest.position import Position
from app.backtest.strategy import (BarContext, BaseStrategy, Signal,
                                   get_strategy)

# ---------------------------------------------------------------------------
# 规则基类与注册机制
# ---------------------------------------------------------------------------

# PARAM_META 支持的控件类型
#   select：枚举单选，取 options 之一
#   multi ：枚举多选，值为逗号分隔字符串；allow_empty=True 时空串是合法语义
#   time  ：HH:MM 时刻
# 未声明的参数由前端按默认值类型推断（数字→number，其余→文本）


def _opts(*pairs: Tuple[str, str]) -> List[dict]:
    """构造枚举选项：_opts(("on","开启"), ("off","关闭"))"""
    return [{"value": v, "label": label} for v, label in pairs]


class BuyRule(ABC):
    """买入规则基类：on_bar 返回 buy 信号或 None（由组合层保证无持仓时调用）"""

    key: str = ""
    name: str = ""
    desc: str = ""
    default_params: Dict = {}
    PARAM_LABELS: Dict[str, str] = {}
    PARAM_META: Dict[str, dict] = {}

    # 实时信号声明：想在实时盘上跑就把这行写出来（留空 dict = 不接入实时）。
    # 声明随规则一起被 app/rule_signals.register_rule_signals 收集——内置规则
    # 服务启动时生效，user_rules/ 里的自定义规则随 reload_rules.bat 热加载
    # 生效，无需再改 config.py 重启服务。
    #   key      实时信号 key，必须唯一；省略时默认用规则 key
    #   period   周期，必须是 config.REALTIME_PERIODS 里的周期
    #   confirm_on_close  True=买入战法（判定最后一根已收盘K线，报了不撤）
    #                     False=盘中异动（判定末根进行中K线，即时、会撤）
    #   interval  仅盘中异动生效：每只股票最多每 N 秒求值一次（默认 30）
    #   params    覆盖规则默认参数（扁平，与验证台参数面板一致）
    #   enabled   默认是否启用
    #   name/desc/fresh_only  可选，缺省用规则自身的 name/desc
    #
    # 例：REALTIME_SIGNAL = {"period": "30m", "confirm_on_close": True}
    REALTIME_SIGNAL: Dict = {}

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
    PARAM_META: Dict[str, dict] = {}

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


_user_loaded = False


def _ensure_user_rules(force: bool = False) -> dict:
    """惰性加载 config.USER_RULES_DIR 下的用户脚本

    在函数体内 import 而非模块顶部：用户脚本会 `from app.backtest.rules import
    BuyRule`，模块级导入会造成 rules → user_rules → rules 的循环导入
    （与 strategy.py 的 _ensure_registered 同一手法）
    """
    global _user_loaded
    if _user_loaded and not force:
        from app.backtest.user_rules import last_report
        return last_report()
    from app.backtest.user_rules import load_user_rules
    _user_loaded = True
    return load_user_rules()


def reload_user_rules() -> dict:
    """强制重新加载用户脚本，返回加载报告（供重载端点/CLI 调用）"""
    return _ensure_user_rules(force=True)


def list_rules() -> dict:
    """列出全部买入/卖出规则（默认参数 + 中文名 + 控件元信息），供前端渲染"""
    _ensure_user_rules()

    def fmt(reg: Dict[str, Type]) -> List[dict]:
        return [{"key": cls.key, "name": cls.name, "desc": cls.desc,
                 "default_params": dict(cls.default_params or {}),
                 "param_labels": dict(getattr(cls, "PARAM_LABELS", {})),
                 "param_meta": dict(getattr(cls, "PARAM_META", {}))}
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
    desc = ("近N根内KDJ与RSI双金叉，买入时间窗[after,before]内，趋势上涨回调/横盘/"
            "下落企稳，阳包阴>阴包阳；低位/量能/涨幅过滤")
    REALTIME_SIGNAL = {
        "key": "rt_kdj_rsi_30m", "period": "30m",
        "confirm_on_close": True, "params": {}, "enabled": True,
    }
    default_params = {
        "buy_amount": 10000.0,      # 每笔固定买入金额（元）
        "kdj_n": 9,                 # KDJ 周期
        "rsi_fast": 12,             # RSI 快线周期
        "rsi_slow": 24,             # RSI 慢线周期
        "golden_window": 3,         # 双金叉判定窗口（根）
        "after": "11:00",           # 最早买入时间 HH:MM
        "before": "15:00",          # 最晚买入时间 HH:MM（15:00 = 含收盘那根）
        # 放行的趋势类型，逗号分隔；留空=不做趋势过滤。
        # 可选 steady_up/up_pullback/sideways/down_stabilize/other
        "trends": "up_pullback,sideways,down_stabilize",
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
        "before": "最晚买入时间", "trends": "放行趋势(留空=不过滤)",
        "pattern_days": "形态统计天数", "max_k": "金叉K值上限(0关)",
        "max_rsi": "金叉RSI上限(0关)", "volume_ratio": "量能倍数(0关)",
        "max_gain_pct": "窗口涨幅上限%(0关)",
    }
    PARAM_META = {
        "after": {"type": "time"},
        "before": {"type": "time"},
        "trends": {"type": "multi", "allow_empty": True,
                   "options": _opts(*cond.TREND_TYPES),
                   "hint": "全不选 = 不做趋势过滤"},
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

        # 5. 时间条件（双边窗口，before 必须显式传，否则会取到函数默认上限）
        hit, r = cond.time_between(ctx.bars, ctx.i,
                                   after=str(self.params["after"]),
                                   before=str(self.params.get("before") or "15:00"))
        if not hit:
            return None
        reasons.append(r)

        # 6. 趋势过滤（trends 留空=关闭）
        allow = {t.strip() for t in str(self.params.get("trends") or "").split(",")
                 if t.strip()}
        if allow:
            trend = cond.classify_trend(ctx.bars, ctx.i)
            if trend not in allow:
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
    desc = "KDJ 金叉买入，可加买入时间窗[after,before]与低位过滤"
    default_params = {"buy_amount": 10000.0, "n": 9,
                      "after": "09:30", "before": "15:00", "max_k": 0.0}
    PARAM_LABELS = {"buy_amount": "每笔买入金额(元)", "n": "KDJ周期",
                    "after": "最早买入时间", "before": "最晚买入时间",
                    "max_k": "金叉K值上限(0关)"}
    PARAM_META = {"after": {"type": "time"}, "before": {"type": "time"}}

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
        hit, _ = cond.time_between(ctx.bars, ctx.i,
                                   after=str(self.params["after"]),
                                   before=str(self.params.get("before") or "15:00"))
        if not hit:
            return None
        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason=f"KDJ金叉 @ {ctx.bar()['ts']}")


# ---------------------------------------------------------------------------
# 建仓检测类规则
# ---------------------------------------------------------------------------


@register_buy("accumulation_detect")
class AccumulationBuy(BuyRule):
    """越千山建仓指标：检测主力建仓行为（BOLL通道+量价+低位+低点抬升）"""
    key = "accumulation_detect"
    name = "建仓信号检测"
    desc = ("基于越千山建仓指标方法论，四维度共振检测建仓行为：\n"
            "1) BOLL下轨上移(成本中枢上移) 2) 近期局部低点逐步抬高(底部承接)\n"
            "3) 价格处于长周期低位(C/LLV<阈值) 4) 上涨放量下跌缩量(量价结构)")
    default_params = {
        "buy_amount": 10000.0,
        "boll_n": 20,            # BOLL 周期
        "boll_lookback": 20,     # BOLL 下轨上移回看根数
        "low_window": 7,         # 局部低点判定窗口
        "low_lookback": 90,      # 低点抬升检测回看范围
        "position_lookback": 200,  # 价格位置判定回看范围
        "position_pct": 35.0,    # 价格位置上限%(在长周期区间的百分位)
        "vol_ratio": 1.0,        # 量价比阈值(上涨均量/下跌均量, 0=关闭)
        "vol_days": 30,          # 量价统计天数
        "min_conditions": 3,     # 最少满足条件数(1-4)
    }
    PARAM_LABELS = {
        "buy_amount": "每笔买入金额(元)", "boll_n": "BOLL周期",
        "boll_lookback": "BOLL下轨回看根数", "low_window": "局部低点窗口",
        "low_lookback": "低点抬升回看范围", "position_lookback": "价格位置回看",
        "position_pct": "价格位置上限%", "vol_ratio": "量价比阈值(0关)",
        "vol_days": "量价统计天数", "min_conditions": "最少满足条件数",
    }
    PARAM_META = {
        "min_conditions": {"type": "select", "options": _opts(
            ("2", "2个条件"), ("3", "3个条件(推荐)"), ("4", "4个条件全满足"))},
    }

    def reset(self) -> None:
        super().reset()
        self._mid = self._upper = self._lower = []
        self._local_low_idx = []

    def prepare(self, bars: list) -> None:
        closes = [k["close"] for k in bars]
        n = int(self.params.get("boll_n", 20))
        self._mid, self._upper, self._lower = ta.boll(closes, n)

        # 预计算局部低点索引（7日窗口）
        w = int(self.params.get("low_window", 7))
        lows = [k["low"] for k in bars]
        self._local_low_idx = []
        for j in range(w, len(bars) - 1):
            left = lows[max(0, j - w):j]
            right = lows[j + 1:min(len(bars), j + w + 1)]
            if left and right and lows[j] <= min(left) and lows[j] <= min(right):
                self._local_low_idx.append(j)

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        i = ctx.i
        reasons = []
        met = 0

        # --- 条件1: BOLL下轨上移 ---
        lookback = int(self.params.get("boll_lookback", 20))
        if (i >= lookback and self._lower[i] is not None
                and self._lower[i - lookback] is not None):
            if self._lower[i] > self._lower[i - lookback]:
                met += 1
                reasons.append("BOLL下轨上移")

        # --- 条件2: 近期局部低点逐步抬高 ---
        low_lb = int(self.params.get("low_lookback", 90))
        recent = [idx for idx in self._local_low_idx
                  if i - low_lb <= idx <= i - 3]
        if len(recent) >= 2:
            prev_lows = [bars_j["low"] for bars_j in
                         [ctx.bars[r] for r in recent[-2:]]]
            if prev_lows[-1] > prev_lows[0]:
                met += 1
                reasons.append(f"低点抬高({prev_lows[0]:.2f}→{prev_lows[-1]:.2f})")

        # --- 条件3: 价格处于长周期低位 ---
        pos_lb = int(self.params.get("position_lookback", 200))
        start = max(0, i - pos_lb)
        hhv = max(k["high"] for k in ctx.bars[start:i + 1])
        llv = min(k["low"] for k in ctx.bars[start:i + 1])
        if hhv > llv:
            pos_pct = (ctx.bar()["close"] - llv) / (hhv - llv) * 100
            threshold = float(self.params.get("position_pct", 35))
            if pos_pct <= threshold:
                met += 1
                reasons.append(f"低位{pos_pct:.0f}%")

        # --- 条件4: 量价结构(上涨放量下跌缩量) ---
        vr_threshold = float(self.params.get("vol_ratio", 1.0) or 0)
        if vr_threshold > 0:
            vol_days = int(self.params.get("vol_days", 30))
            vs = max(0, i - vol_days)
            up_vol, down_vol = [], []
            for j in range(vs + 1, i + 1):
                chg = ctx.bars[j]["close"] - ctx.bars[j - 1]["close"]
                if chg > 0:
                    up_vol.append(ctx.bars[j]["volume"])
                elif chg < 0:
                    down_vol.append(ctx.bars[j]["volume"])
            if up_vol and down_vol:
                avg_up = sum(up_vol) / len(up_vol)
                avg_dn = sum(down_vol) / len(down_vol)
                if avg_dn > 0 and avg_up / avg_dn >= vr_threshold:
                    met += 1
                    reasons.append(f"量比{avg_up / avg_dn:.1f}")

        # --- 判定: 满足条件数 >= 阈值 ---
        min_cond = int(self.params.get("min_conditions", 3))
        if met >= min_cond:
            return Signal("buy", amount=float(self.params["buy_amount"]),
                          reason="建仓信号：" + "，".join(reasons))
        return None


# ---------------------------------------------------------------------------
# 盘中异动类规则
#
# 这四条原本是实时侧写死的"快照策略"，读腾讯快照的 price/change_pct/volume/
# limit_up 等字段，因此永远无法回测。改写成纯K线表达后它们既能上实时盘、
# 也能在验证台跑历史——关键是**未收盘那根K线本身就携带当日实时信息**：
# 末根的 volume 就是当日累计量、close 就是当前价。
#
# 配 confirm_on_close=False 时判定末根（进行中）→ 盘中即时语义；
# 配 True 时判定最后一根已收盘 → 与回测逐根一致。两种模式见
# config.REALTIME_RULE_SIGNALS
# ---------------------------------------------------------------------------


def _day_change_pct(bars: list, i: int) -> Optional[float]:
    """当日涨幅%：(今收 - 昨收) / 昨收。前复权序列上前后两根同基准，比值成立"""
    if i < 1:
        return None
    prev = bars[i - 1]["close"]
    return (bars[i]["close"] - prev) / prev * 100.0 if prev else None


def _limit_pct_by_code(code: str) -> float:
    """按代码前缀推涨停幅度%：创业板/科创板 20%，其余 10%

    近似判据，不等于交易所口径：ST 股是 5%、上市首日不设限、北交所 30%。
    实时侧原来读快照的 limit_up 字段（交易所给的真实涨停价），改成纯K线表达
    后只能这样推——换来的是可回测
    """
    return 20.0 if code.startswith(("30", "68")) else 10.0


@register_buy("volume_breakout")
class VolumeBreakoutBuy(BuyRule):
    """放量突破：量比≥阈值 且 价格突破前 N 根最高"""
    key = "volume_breakout"
    name = "放量突破"
    desc = "量比≥阈值 且 收盘突破前N根最高（量比=当根量/前5根均量）"
    REALTIME_SIGNAL = {
        "key": "volume_breakout", "period": "daily",
        "confirm_on_close": False, "params": {}, "enabled": True,
    }
    default_params = {"buy_amount": 10000.0, "volume_ratio": 2.0, "break_days": 20}
    PARAM_LABELS = {"buy_amount": "每笔买入金额(元)", "volume_ratio": "量比阈值",
                    "break_days": "突破回看根数"}

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        i, bars = ctx.i, ctx.bars
        days = int(self.params["break_days"])
        if i < max(days, 6):
            return None
        bar = bars[i]
        base = [k["volume"] for k in bars[i - 5:i]]          # 前5根均量（不含当根）
        avg = sum(base) / len(base) if base else 0.0
        if avg <= 0:
            return None
        ratio = bar["volume"] / avg
        if ratio < float(self.params["volume_ratio"]):
            return None
        top = max(k["high"] for k in bars[i - days:i])       # 前N根最高（不含当根）
        if not (top > 0 and bar["close"] > top):
            return None
        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason=f"量比{ratio:.1f}倍，{bar['close']:.2f}突破{days}根高点{top:.2f}")


@register_buy("rsi_oversold")
class RsiOversoldBuy(BuyRule):
    """RSI超卖反弹：RSI<阈值 且 当根转涨"""
    key = "rsi_oversold"
    name = "RSI超卖反弹"
    desc = "RSI低于阈值 且 当根收盘高于上一根（超卖后转涨）"
    REALTIME_SIGNAL = {
        "key": "rsi_oversold", "period": "daily",
        "confirm_on_close": False, "params": {}, "enabled": True,
    }
    default_params = {"buy_amount": 10000.0, "rsi_n": 14, "rsi_below": 30.0}
    PARAM_LABELS = {"buy_amount": "每笔买入金额(元)", "rsi_n": "RSI周期",
                    "rsi_below": "RSI上限"}

    def reset(self) -> None:
        super().reset()
        self._r = []

    def prepare(self, bars: list) -> None:
        self._r = ta.rsi([k["close"] for k in bars], int(self.params["rsi_n"]))

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        i = ctx.i
        if i < 1 or self._r[i] is None:
            return None
        chg = _day_change_pct(ctx.bars, i)
        if chg is None or chg <= 0:
            return None
        if self._r[i] >= float(self.params["rsi_below"]):
            return None
        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason=f"RSI{self.params['rsi_n']}={self._r[i]:.1f}"
                             f"<{self.params['rsi_below']} 且转涨{chg:+.2f}%")


@register_buy("strong_up")
class StrongUpBuy(BuyRule):
    """强势拉升：涨幅≥阈值 且 非一字板"""
    key = "strong_up"
    name = "强势拉升"
    desc = "涨幅≥阈值 且 非一字板（四价相等视为一字板，无法买入）"
    REALTIME_SIGNAL = {
        "key": "strong_up", "period": "daily",
        "confirm_on_close": False, "params": {}, "enabled": True,
    }
    default_params = {"buy_amount": 10000.0, "pct": 5.0}
    PARAM_LABELS = {"buy_amount": "每笔买入金额(元)", "pct": "涨幅阈值%"}

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        bar, chg = ctx.bar(), _day_change_pct(ctx.bars, ctx.i)
        if chg is None or chg < float(self.params["pct"]):
            return None
        # 一字板：开=高=低=收，全天封死买不进。原实现靠快照的 limit_up
        # 比较开盘价，这里用四价相等判定，不需要涨停价
        if bar["open"] == bar["high"] == bar["low"] == bar["close"]:
            return None
        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason=f"涨幅{chg:+.2f}%，价{bar['close']:.2f}")


@register_buy("limit_up")
class LimitUpBuy(BuyRule):
    """涨停预警：涨幅接近涨停幅度"""
    key = "limit_up"
    name = "涨停预警"
    desc = "涨幅达涨停幅度的99%以上；幅度按代码前缀推（创业板/科创板20%，其余10%）"
    REALTIME_SIGNAL = {
        "key": "limit_up", "period": "daily",
        "confirm_on_close": False, "interval": 3, "params": {}, "enabled": True,
    }
    default_params = {"buy_amount": 10000.0, "limit_pct": 0.0}
    PARAM_LABELS = {"buy_amount": "每笔买入金额(元)",
                    "limit_pct": "涨停幅度%(0=按代码自动)"}

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        chg = _day_change_pct(ctx.bars, ctx.i)
        if chg is None:
            return None
        limit = float(self.params.get("limit_pct") or 0) or _limit_pct_by_code(ctx.code)
        if chg < limit * 0.99:
            return None
        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason=f"涨幅{chg:+.2f}% 触及涨停(约{limit:.0f}%)，"
                             f"价{ctx.bar()['close']:.2f}")


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
    PARAM_META = {
        "dead_cross_confirm": {"type": "select", "options": _opts(
            ("none", "不确认（死叉即卖）"),
            ("ma10", "收盘跌破MA10"),
            ("peak", "自高点回撤达阈值"),
            ("any", "跌破MA10 或 回撤达阈值"))},
        "rsi_dead_cross": {"type": "select", "options": _opts(
            ("on", "开启（死叉即卖）"),
            ("strict", "严格（还需收盘跌破MA5）"),
            ("off", "关闭"))},
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
                    "rsi_dead_cross": "RSI死叉"}
    # 本规则只判断是否等于 "on"，故只有开/关两态
    PARAM_META = {"rsi_dead_cross": {"type": "select", "options": _opts(
        ("on", "开启（RSI死叉也卖）"), ("off", "关闭"))}}

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


@register_sell("accumulation_exit")
class AccumulationExitSell(SellRule):
    """建仓策略卖出：BOLL下轨拐头+固定止损+MACD死叉"""
    key = "accumulation_exit"
    name = "建仓退出信号"
    desc = ("建仓策略配套卖出规则：\n"
            "1) 固定止损(跌破买入价N%) 2) BOLL下轨拐头向下(通道破坏)\n"
            "3) MACD死叉确认")
    default_params = {
        "stop_loss_pct": 8.0,
        "boll_n": 20,
        "boll_turn_lookback": 5,   # BOLL下轨拐头回看根数
        "macd_death": "on",        # MACD死叉: on/off
        "fast": 12, "slow": 26, "signal": 9,
    }
    PARAM_LABELS = {
        "stop_loss_pct": "固定止损%", "boll_n": "BOLL周期",
        "boll_turn_lookback": "BOLL拐头回看", "macd_death": "MACD死叉",
        "fast": "MACD快线", "slow": "MACD慢线", "signal": "MACD信号",
    }
    PARAM_META = {
        "macd_death": {"type": "select", "options": _opts(
            ("on", "开启"), ("off", "关闭"))},
    }

    def reset(self) -> None:
        super().reset()
        self._lower = []
        self._dif = self._dea = []

    def prepare(self, bars: list) -> None:
        closes = [k["close"] for k in bars]
        n = int(self.params.get("boll_n", 20))
        _, _, self._lower = ta.boll(closes, n)
        self._dif, self._dea, _ = ta.macd(
            closes, int(self.params.get("fast", 12)),
            int(self.params.get("slow", 26)), int(self.params.get("signal", 9)))

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        pos: Position = ctx.position
        bar = ctx.bar()
        i = ctx.i

        # 1. 固定止损
        sl_pct = float(self.params.get("stop_loss_pct", 8.0) or 0)
        if sl_pct > 0:
            sl = pos.buy_price * (1 - sl_pct / 100.0)
            if bar["low"] <= sl:
                return Signal("sell", price=sl,
                              reason=f"跌破{sl_pct}%止损({sl:.2f})")

        # 2. BOLL下轨拐头向下（近N根从上升到下降）
        turn = int(self.params.get("boll_turn_lookback", 5))
        if (i >= turn + 1 and self._lower[i] is not None
                and self._lower[i - turn] is not None):
            # 当前下轨低于turn根前 → 通道走坏
            if self._lower[i] < self._lower[i - turn]:
                # 确认之前是上升的（再往前turn根）
                if (i >= turn * 2 and self._lower[i - turn] is not None
                        and self._lower[i - turn * 2] is not None
                        and self._lower[i - turn] > self._lower[i - turn * 2]):
                    return Signal("sell",
                                  reason=f"BOLL下轨拐头({self._lower[i - turn]:.2f}→{self._lower[i]:.2f})")

        # 3. MACD死叉
        if self.params.get("macd_death", "on") == "on" and i >= 1:
            d0, e0 = self._dif[i - 1], self._dea[i - 1]
            d1, e1 = self._dif[i], self._dea[i]
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
    _ensure_user_rules()        # 用户脚本也能被 --buy-rule / 验证台选中
    buy_key = getattr(cfg, "buy_rule", "") or ""
    sell_key = getattr(cfg, "sell_rule", "") or ""
    if buy_key and sell_key:
        st = ComboStrategy(buy_key, sell_key)
    else:
        st = get_strategy(getattr(cfg, "strategy", "") or "all_in_all_out")
    st.reset()
    st.params = {**st.default_params, **(getattr(cfg, "params", None) or {})}
    return st
