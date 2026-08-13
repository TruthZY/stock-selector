# -*- coding: utf-8 -*-
"""把回测买入规则接到实时盘信号侦测

回测里验证过的买入战法（BuyRule，含 user_rules/ 里用户自己写的）在这里被包装成
实时策略引擎认识的形状 —— **实时信号全部来自这里**，原先写死的内置策略已删除。
两边共用同一份规则代码，不会出现"回测一套、实时另一套"的逻辑漂移。

只支持买入规则：卖出规则的契约是"持仓中调用"，需要 ctx.position，实时侧没有持仓。

三个必须做对的地方（都有实测依据）：

1. **两种确认模式，语义完全不同。** 数据源会返回进行中的当前K线——实测 11:16 时
   30m 末根 ts=11:30（覆盖 11:00-11:30），成交量只有已收盘那根的三成。
   - `confirm_on_close=True`（买入战法）：判定最后一根已收盘K线（ts <= 现在），
     与回测逐根一致、报了不撤。实测全池 60m 历史，用"半根"判断会有 15.9% 的
     信号到收盘就不成立
   - `confirm_on_close=False`（盘中异动）：刻意判定末根（进行中），换取低延迟。
     会撤，只作提醒。日线末根在盘中被实时快照刷新到 3 秒新鲜
     （见 Scanner._live_daily_bar）

2. **参数自己建。** 规则信号的参数来自 config.REALTIME_RULE_SIGNALS 的 params，
   与引擎 evaluate 传进来的第二个入参无关，所以适配器忽略它、自建扁平参数。

3. **规则实例必须按股票隔离。** 规则把指标缓存成按绝对下标索引的序列
   （self._k[ctx.i]），跨股票共用实例会读到别人的指标值甚至 IndexError。
"""
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import config
from app import strategies as strat
from app.bars import last_closed_index

# 各周期"新鲜度"上限（分钟）：已收盘K线早于此值则不再触发，
# 避免服务重启后对着昨天的收盘K线补报一批信号
_FRESH_MINUTES = {"30m": 60, "60m": 120, "daily": 24 * 60}
_FRESH_DEFAULT = 120


def _age_minutes(ts: str, now: Optional[datetime] = None) -> float:
    """该K线结束时刻距今多少分钟（解析失败返回 0，按"新鲜"处理，不误杀）"""
    now = now or datetime.now()
    try:
        if len(ts) <= 10:
            end = datetime.strptime(ts[:10], "%Y-%m-%d")
        else:
            end = datetime.strptime(ts[:16], "%Y-%m-%d %H:%M")
        return (now - end).total_seconds() / 60.0
    except ValueError:
        return 0.0


class RuleSignal:
    """把一个买入规则包装成实时策略：签名对齐 STRATEGY_IMPL 的 fn(ctx) -> (命中, 原因)"""

    def __init__(self, key: str, rule_key: str, period: str, params: Dict,
                 name: str = "", desc: str = "", fresh_only: bool = True,
                 confirm_on_close: bool = True, interval: float = 30.0):
        self.key = key
        self.rule_key = rule_key
        self.period = period
        self.params = dict(params or {})
        self.name = name
        self.desc = desc
        self.fresh_only = fresh_only
        # True：判定最后一根已收盘K线（与回测逐根一致，报了不撤）
        # False：判定末根（进行中）—— 盘中即时语义，会撤，用于异动提醒
        self.confirm_on_close = confirm_on_close
        # 盘中模式下的求值间隔（秒）。挂在 3 秒的快照循环上，但每只股票
        # 最多每 interval 秒求值一次——盘中模式每次都要重算 prepare()，
        # 实测全池一轮 12~65ms，每 3 秒跑一遍是 0.4%~2.2% CPU 且阻塞事件循环
        self.interval = max(0.0, float(interval))
        self._rules: Dict[str, tuple] = {}      # code -> (规则实例, 缓存键)
        self._fired: Dict[str, str] = {}        # code -> 最近已报的K线 ts
        self._last_eval: Dict[str, float] = {}  # code -> 上次求值时刻（按股票节流）
        self._errors: Dict[str, int] = {}       # "code/异常类型" -> 次数
        self.last_error = ""
        self.fired_count = 0
        self.eval_count = 0
        self.skip_count = 0                     # 因未到 interval 而跳过的次数
        self.prepare_count = 0

    # ------------------------- 规则实例 -------------------------

    def _rule_class(self):
        """每次现查注册表而不缓存类：规则可能被 reload_rules 删除或替换"""
        from app.backtest.rules import BUY_REGISTRY
        return BUY_REGISTRY.get(self.rule_key)

    def _cache_key(self, bars: List[dict]):
        """prepare 结果的缓存键

        收盘确认模式用末根 ts 就够——已收盘K线不可变。
        但盘中模式**不能用 ts**：末根的 ts 一整天都是"今天"，而 close/volume
        每几秒就在变，用 ts 做键会让缓存永不失效、规则整天读当天第一次算出的
        指标序列。所以盘中模式把末根的值也纳入键（等价于每次重算）
        """
        if not bars:
            return ""
        last = bars[-1]
        if self.confirm_on_close:
            return last["ts"]
        return (last["ts"], last["close"], last["high"], last["low"], last["volume"])

    def _rule_for(self, code: str, bars: List[dict]):
        """取该股的规则实例；缓存键变化才重新 prepare（prepare 是 O(n) 全量重算）"""
        cls = self._rule_class()
        if cls is None:
            return None
        last_ts = self._cache_key(bars)
        cached = self._rules.get(code)
        if cached is not None and cached[1] == last_ts and type(cached[0]) is cls:
            return cached[0]
        rule = cls()
        # 顺序要紧：reset() 会把 params 重置成 default_params，
        # 必须先 reset 再注入覆盖参数，最后 prepare（与 build_strategy 同序）
        rule.reset()
        rule.params = {**(cls.default_params or {}), **self.params}
        rule.prepare(bars)
        self.prepare_count += 1
        self._rules[code] = (rule, last_ts)
        return rule

    # ------------------------- 求值 -------------------------

    def __call__(self, ctx, engine_params: Optional[Dict] = None) -> Tuple:
        """签名对齐 STRATEGY_IMPL 的 fn(ctx, params)

        engine_params 是留给"非规则实现"的口子，规则信号的参数来自
        config.REALTIME_RULE_SIGNALS，所以这里刻意忽略它
        """
        # 按股票节流，只对盘中模式生效：收盘确认模式跑在 30 秒的K线循环上、
        # 且有"每根只报一次"去重，再套一层 30 秒节流会因为循环抖动（29.9 秒）
        # 误跳过整轮。必须按股票而不是按信号全局记时：全局记的话一轮里
        # 第一只会重置计时器、其余 120 只被跳过整个 interval
        if not self.confirm_on_close and self.interval > 0:
            now = time.time()
            if now - self._last_eval.get(ctx.code, 0.0) < self.interval:
                self.skip_count += 1
                return False, "", ""
            self._last_eval[ctx.code] = now
        self.eval_count += 1
        try:
            return self._evaluate(ctx)
        except Exception as e:
            # 实时引擎的 evaluate 用裸 except 吞异常，规则报错会毫无线索地静默失效，
            # 所以这里自己捕获、按 (股票, 异常类型) 去重后打日志并计数
            tag = f"{ctx.code}/{type(e).__name__}"
            self._errors[tag] = self._errors.get(tag, 0) + 1
            self.last_error = f"{ctx.code} {type(e).__name__}: {e}"
            if self._errors[tag] == 1:
                print(f"[rule_signals] {self.key} 规则异常（同类不再重复报告）："
                      f"{self.last_error}", flush=True)
            return False, "", ""

    def _evaluate(self, ctx) -> Tuple:
        """返回 (命中, 原因, bar_ts)

        bar_ts 决定 Scanner 用哪种去重：收盘确认模式给出判定的K线 ts →
        "每根只报一次"；盘中模式给空串 → 走 SIGNAL_DEDUP_SECONDS 时间窗
        （日线一根持续一整天，按根去重会让涨停一天只报一次）
        """
        from app.backtest.strategy import BarContext
        bars = (ctx.bars or {}).get(self.period) or []
        if len(bars) < 2:
            return False, f"{self.period} 数据不足", ""

        if self.confirm_on_close:
            i = last_closed_index(bars)
            if i is None:
                return False, "无已收盘K线", ""
            ts = bars[i]["ts"]
            # 每根已收盘K线只报一次。DB 那层 30 分钟去重不够用：
            # 30m 的一根正好是 30 分钟，边界上会漏报或重报
            if self._fired.get(ctx.code) == ts:
                return False, "本根已报", ""
            if self.fresh_only:
                limit = _FRESH_MINUTES.get(self.period, _FRESH_DEFAULT)
                if _age_minutes(ts) > limit:
                    return False, f"K线过旧（{ts}）", ""
        else:
            # 盘中模式：判定末根（进行中）。不做"每根只报一次"——那是收盘确认的
            # 语义，日线一根能持续一整天，按根去重会让涨停一天只报一次。
            # 交给 Scanner 的 SIGNAL_DEDUP_SECONDS 时间窗去重。
            # 新鲜度检查也跳过：末根本来就是当下
            i = len(bars) - 1
            ts = bars[i]["ts"]

        rule = self._rule_for(ctx.code, bars)
        if rule is None:
            return False, f"规则 {self.rule_key} 已卸载", ""
        sig = rule.on_bar(BarContext(ctx.code, ctx.name, bars, i, None, rule.params))
        if sig is None or getattr(sig, "action", "") != "buy":
            return False, "", ""
        if self.confirm_on_close:
            self._fired[ctx.code] = ts
        self.fired_count += 1
        # 盘中模式 bar_ts 传空：末根一整天不变，按根去重会让信号一天只报一次
        return True, f"{sig.reason}（{self.period} {ts}）", (ts if self.confirm_on_close else "")

    # ------------------------- 观测 -------------------------

    def stats(self) -> dict:
        return {
            "key": self.key, "rule": self.rule_key, "period": self.period,
            "name": self.name, "params": dict(self.params),
            "kind": "buy" if self.confirm_on_close else "alert",
            "confirm_on_close": self.confirm_on_close,
            "interval": self.interval if not self.confirm_on_close else None,
            "rule_loaded": self._rule_class() is not None,
            "eval_count": self.eval_count, "fired_count": self.fired_count,
            "skip_count": self.skip_count,
            "prepare_count": self.prepare_count,
            "codes_primed": len(self._rules),
            "last_fired": dict(sorted(self._fired.items())[-5:]),
            "error_count": sum(self._errors.values()),
            "errors": dict(self._errors),
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

# key -> RuleSignal，供 /api/rule-signals 观测与重载时替换
REGISTERED: Dict[str, RuleSignal] = {}


def register_rule_signals(engine) -> dict:
    """把 config.REALTIME_RULE_SIGNALS 注册进实时策略引擎，返回注册报告

    要三处协同才生效，少一处就静默失效：
      STRATEGY_IMPL[key]        求值函数
      两个集合之一 add(key)      evaluate 对未知 key 是拒绝式的（无人用 kind="all"）
                                收盘确认→KLINE_STRATEGIES（K线循环 30s）
                                盘中模式→SNAPSHOT_STRATEGIES（快照循环 3s + interval 节流）
      engine.strategies[key]    决定界面显示与 toggle 能否生效
    """
    # 函数体内 import：app.backtest.__init__ 会连带 eager import engine → httpx/store，
    # 放模块顶层会让实时侧启动凭空背上整个回测栈
    from app.backtest.rules import BUY_REGISTRY, _ensure_user_rules
    _ensure_user_rules()        # 用户脚本里的规则要先注册进 BUY_REGISTRY

    report = {"loaded": [], "failed": [], "ts": time.time()}

    # 先摘掉上一轮注册的（支持重载）
    for key in list(REGISTERED):
        REGISTERED.pop(key, None)
        strat.STRATEGY_IMPL.pop(key, None)
        strat.KLINE_STRATEGIES.discard(key)
        strat.SNAPSHOT_STRATEGIES.discard(key)
        engine.strategies.pop(key, None)

    for spec in (config.REALTIME_RULE_SIGNALS or []):
        key = str(spec.get("key") or "").strip()
        rule_key = str(spec.get("rule") or "").strip()
        period = str(spec.get("period") or "").strip()

        def fail(reason: str):
            report["failed"].append({"key": key or "(未命名)", "rule": rule_key,
                                     "period": period, "error": reason})
        if not key:
            fail("缺少 key")
            continue
        if key in REGISTERED:
            fail("key 重复")
            continue
        if rule_key not in BUY_REGISTRY:
            fail(f"买入规则 {rule_key!r} 不存在（可用：{', '.join(sorted(BUY_REGISTRY)) or '无'}）")
            continue
        if period not in config.REALTIME_PERIODS:
            fail(f"周期 {period!r} 实时侧没有对应序列"
                 f"（可用：{', '.join(config.REALTIME_PERIODS)}）")
            continue

        cls = BUY_REGISTRY[rule_key]
        # name 不带周期：周期单独放 period 字段，否则前端会渲染成
        # 「买入·KDJ+RSI双金叉共振(30m) 30m」
        name = str(spec.get("name") or "") or getattr(cls, "name", rule_key)
        on_close = bool(spec.get("confirm_on_close", True))
        sig = RuleSignal(key=key, rule_key=rule_key, period=period,
                         params=spec.get("params") or {}, name=name,
                         desc=str(spec.get("desc") or "") or getattr(cls, "desc", ""),
                         fresh_only=bool(spec.get("fresh_only", True)),
                         confirm_on_close=on_close,
                         interval=float(spec.get("interval", 30.0)))
        REGISTERED[key] = sig
        strat.STRATEGY_IMPL[key] = sig
        # 决定挂哪个循环：收盘确认随K线同步（30s），盘中模式随快照轮询（3s）
        # 再由 interval 按股票节流
        (strat.KLINE_STRATEGIES if on_close else strat.SNAPSHOT_STRATEGIES).add(key)
        kind = "buy" if on_close else "alert"
        engine.strategies[key] = {
            "enabled": bool(spec.get("enabled", True)),
            "name": name, "desc": sig.desc,
            "kind": kind, "period": period, "rule": rule_key,
        }
        report["loaded"].append({"key": key, "rule": rule_key, "period": period,
                                 "name": name, "kind": kind,
                                 "confirm_on_close": on_close,
                                 "interval": sig.interval if not on_close else None,
                                 "params": dict(sig.params)})
    return report


def stats_report() -> dict:
    """全部已注册实时规则信号的运行状况"""
    return {"signals": [s.stats() for s in REGISTERED.values()]}
