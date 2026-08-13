# -*- coding: utf-8 -*-
"""把回测买入规则接到实时盘信号侦测

回测里验证过的买入战法（BuyRule，含 user_rules/ 里用户自己写的）在这里被包装成
实时策略引擎认识的形状，与内置 8 个策略并列出现在首页策略条上、可开关、命中即推送。
两边共用同一份规则代码，不会出现"回测一套、实时另一套"的逻辑漂移。

只支持买入规则：卖出规则的契约是"持仓中调用"，需要 ctx.position，实时侧没有持仓。

三个必须做对的地方（都有实测依据）：

1. **绝不能评估未收盘的那根K线。** 数据源会返回进行中的当前K线——实测 11:16 时
   30m 末根 ts=11:30（覆盖 11:00-11:30），成交量只有已收盘那根的三成。直接评估它
   会让指标在一根K线内反复翻转、信号乱闪。ts 是K线**结束**时刻，故 ts <= 现在
   即已收盘。

2. **参数自己建，不吃引擎那份。** 引擎给内置策略传的是 config.STRATEGY_PARAMS
   的按策略切片；规则信号的参数来自 config.REALTIME_RULE_SIGNALS 的 params，
   两者来源不同，所以适配器忽略传入的 params、自建扁平参数。

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
                 name: str = "", desc: str = "", fresh_only: bool = True):
        self.key = key
        self.rule_key = rule_key
        self.period = period
        self.params = dict(params or {})
        self.name = name
        self.desc = desc
        self.fresh_only = fresh_only
        self._rules: Dict[str, tuple] = {}      # code -> (规则实例, 已 prepare 到的末根 ts)
        self._fired: Dict[str, str] = {}        # code -> 最近已报的K线 ts
        self._errors: Dict[str, int] = {}       # "code/异常类型" -> 次数
        self.last_error = ""
        self.fired_count = 0
        self.eval_count = 0
        self.prepare_count = 0

    # ------------------------- 规则实例 -------------------------

    def _rule_class(self):
        """每次现查注册表而不缓存类：规则可能被 reload_rules 删除或替换"""
        from app.backtest.rules import BUY_REGISTRY
        return BUY_REGISTRY.get(self.rule_key)

    def _rule_for(self, code: str, bars: List[dict]):
        """取该股的规则实例；序列末根变化才重新 prepare（prepare 是 O(n) 全量重算）"""
        cls = self._rule_class()
        if cls is None:
            return None
        last_ts = bars[-1]["ts"] if bars else ""
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

    def __call__(self, ctx, engine_params: Optional[Dict] = None) -> Tuple[bool, str]:
        """签名对齐 STRATEGY_IMPL 的 fn(ctx, params)

        engine_params 是引擎给内置策略用的那份（来自 config.STRATEGY_PARAMS），
        规则信号有自己的参数来源（config.REALTIME_RULE_SIGNALS 的 params），
        所以这里刻意忽略它
        """
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
            return False, ""

    def _evaluate(self, ctx) -> Tuple[bool, str]:
        from app.backtest.strategy import BarContext
        bars = (ctx.bars or {}).get(self.period) or []
        if len(bars) < 2:
            return False, f"{self.period} 数据不足"
        i = last_closed_index(bars)
        if i is None:
            return False, "无已收盘K线"
        ts = bars[i]["ts"]
        # 每根已收盘K线只报一次。DB 那层 30 分钟去重不够用：
        # 30m 的一根正好是 30 分钟，边界上会漏报或重报
        if self._fired.get(ctx.code) == ts:
            return False, "本根已报"
        if self.fresh_only:
            limit = _FRESH_MINUTES.get(self.period, _FRESH_DEFAULT)
            if _age_minutes(ts) > limit:
                return False, f"K线过旧（{ts}）"
        rule = self._rule_for(ctx.code, bars)
        if rule is None:
            return False, f"规则 {self.rule_key} 已卸载"
        sig = rule.on_bar(BarContext(ctx.code, ctx.name, bars, i, None, rule.params))
        if sig is None or getattr(sig, "action", "") != "buy":
            return False, ""
        self._fired[ctx.code] = ts
        self.fired_count += 1
        return True, f"{sig.reason}（{self.period} {ts}）"

    # ------------------------- 观测 -------------------------

    def stats(self) -> dict:
        return {
            "key": self.key, "rule": self.rule_key, "period": self.period,
            "name": self.name, "params": dict(self.params),
            "rule_loaded": self._rule_class() is not None,
            "eval_count": self.eval_count, "fired_count": self.fired_count,
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

    要四处协同才生效，少一处就静默失效：
      STRATEGY_IMPL[key]        求值函数
      KLINE_STRATEGIES.add(key) evaluate 对未知 key 是拒绝式的（无人用 kind="all"）
      engine.strategies[key]    决定界面显示与 toggle 能否生效
    """
    # 函数体内 import：app.backtest.__init__ 会连带 eager import engine → httpx/store，
    # 放模块顶层会让实时侧启动凭空背上整个回测栈
    from app.backtest.rules import BUY_REGISTRY, _ensure_user_rules
    _ensure_user_rules()        # 用户脚本里的规则要先注册进 BUY_REGISTRY

    report = {"loaded": [], "failed": [], "ts": time.time()}
    builtin_keys = set(config.DEFAULT_STRATEGIES)

    # 先摘掉上一轮注册的（支持重载）
    for key in list(REGISTERED):
        REGISTERED.pop(key, None)
        strat.STRATEGY_IMPL.pop(key, None)
        strat.KLINE_STRATEGIES.discard(key)
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
        if key in builtin_keys:
            fail(f"key 与内置策略冲突（{key}），请改名")
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
        name = str(spec.get("name") or "") or f"{getattr(cls, 'name', rule_key)}({period})"
        sig = RuleSignal(key=key, rule_key=rule_key, period=period,
                         params=spec.get("params") or {}, name=name,
                         desc=str(spec.get("desc") or "") or getattr(cls, "desc", ""),
                         fresh_only=bool(spec.get("fresh_only", True)))
        REGISTERED[key] = sig
        strat.STRATEGY_IMPL[key] = sig
        strat.KLINE_STRATEGIES.add(key)
        engine.strategies[key] = {"enabled": bool(spec.get("enabled", True)),
                                  "name": name, "desc": sig.desc}
        report["loaded"].append({"key": key, "rule": rule_key, "period": period,
                                 "name": name, "params": dict(sig.params)})
    return report


def stats_report() -> dict:
    """全部已注册实时规则信号的运行状况"""
    return {"signals": [s.stats() for s in REGISTERED.values()]}
