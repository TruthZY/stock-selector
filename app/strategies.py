# -*- coding: utf-8 -*-
"""实时选股引擎：策略注册表 + 开关 + 求值分发

**这里不再内置任何策略实现。** 实时信号全部来自 app/rule_signals.py 把回测
买入规则（app/backtest/rules.py 的 BuyRule）包装后注册进来，两条好处：
与回测共用同一份代码不会逻辑漂移，且每个信号都能在验证台跑历史验证。

原先写死在本文件的 8 个策略已删除：
- `ma_bull` / `ma_golden_cross` / `macd_golden` 与买入规则完全重复
  （`macd_golden` 在 BUY_REGISTRY 里本来就有同名规则）
- `volume_breakout` / `rsi_oversold` / `strong_up` / `limit_up` 已改写成纯K线
  规则。它们原本读快照的 price/change_pct/volume/limit_up，因此永远无法回测；
  改写的关键是**未收盘那根K线本身就携带当日实时信息**（末根 volume 就是当日
  累计量、close 就是当前价）
- `low_pe` 放弃：PE 需要每股收益，不在任何K线字段里

本模块保留的职责：
- `StockContext`：单只股票的判断上下文
- `StrategyEngine`：注册表宿主（供 rule_signals 注册）+ 开关 + 按 kind 分发
- `SNAPSHOT_STRATEGIES` / `KLINE_STRATEGIES`：决定信号挂在哪个循环上，
  由 rule_signals 注册时按 confirm_on_close 填充
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class StockContext:
    """单只股票的完整判断上下文"""
    code: str
    name: str
    snap: Optional[dict] = None      # 腾讯实时快照（供 price/change_pct 写进信号）
    # 各周期K线 period -> 升序K线，键取自 config.REALTIME_PERIODS。
    # 盘中日线末根会被实时快照刷新（见 Scanner._live_daily_bar）。
    # 不再单列 daily/k60 别名字段：删掉内置策略后没有消费方，
    # 而且别名与 bars 可能不同步（一个新鲜化过、一个没有）
    bars: Dict[str, List[dict]] = field(default_factory=dict)
    # 刻意不放 params：策略参数由 StrategyEngine.evaluate 作为第二个入参显式传给
    # 实现。这里放一份就会有人拿整个嵌套字典当扁平参数用（曾经就是这么坏掉的）


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

# key -> 求值函数 fn(ctx, params) -> (命中, 原因) 或 (命中, 原因, 判定的K线ts)。
# 由 app/rule_signals.py 在注册时填充，本模块不预置任何实现
STRATEGY_IMPL: Dict[str, Callable[..., tuple]] = {}

# 挂快照轮询（3秒一轮，配 interval 节流）：盘中异动类，判定末根（进行中）
SNAPSHOT_STRATEGIES: set = set()
# 挂K线同步（30秒）：买入战法类，判定最后一根已收盘K线
KLINE_STRATEGIES: set = set()


class StrategyEngine:
    """实时策略引擎：对单只股票执行所有启用的策略，返回命中列表"""

    def __init__(self):
        # key -> {enabled, name, desc, kind, period, rule}
        # 由 rule_signals.register_rule_signals 填充
        self.strategies: Dict[str, dict] = {}

    def toggle(self, key: str, enabled: bool) -> bool:
        if key in self.strategies:
            self.strategies[key]["enabled"] = enabled
            return True
        return False

    def list(self) -> List[dict]:
        return [{"key": k, **v} for k, v in self.strategies.items()]

    def evaluate(self, ctx: StockContext, kind: str = "all") -> List[dict]:
        """对一只股票执行策略，返回 [{"key","name","reason","bar_ts","price","change_pct"}]

        kind 决定跑哪一批：snapshot=盘中异动（快照循环）/ kline=买入战法（K线循环）。
        对不在任何集合里的 key 是拒绝式的——注册时必须加进其中一个集合
        """
        hits = []
        for key, meta in self.strategies.items():
            if not meta.get("enabled"):
                continue
            if kind == "snapshot" and key not in SNAPSHOT_STRATEGIES:
                continue
            if kind == "kline" and key not in KLINE_STRATEGIES:
                continue
            impl = STRATEGY_IMPL.get(key)
            if impl is None:
                continue
            try:
                # 第二个参数留给"非规则实现"用（规则信号自带参数、会忽略它）
                res = impl(ctx, {})
            except Exception:
                continue
            # 实现可返回 (命中, 原因) 或 (命中, 原因, 判定所依据的K线ts)。
            # 带 ts 的（收盘确认类）由 Scanner 按"每根只报一次"去重；
            # 不带的（盘中类）走 SIGNAL_DEDUP_SECONDS 时间窗
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
