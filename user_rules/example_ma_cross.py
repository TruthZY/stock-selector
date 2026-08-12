# -*- coding: utf-8 -*-
"""自定义战法示例：均线金叉买入 + 均线死叉/止损卖出

复制这个文件改名就能写自己的战法。改完跑 reload_rules.bat 生效，不用重启服务。

════════════════════════════════════════════════════════════════════
最重要的一条：指标必须在 prepare() 里算一次
════════════════════════════════════════════════════════════════════

prepare(bars) 每只股票只调一次，拿到的是完整K线序列（含预热期）；
on_bar(ctx) 每根K线调一次，几十万次量级，所以里面只能做 O(1) 的取值比较。

反面写法（会被性能门禁拒绝）：

    from app.backtest import conditions as cond
    def on_bar(self, ctx):
        hit, _ = cond.kdj_golden_cross(ctx.bars, ctx.i)   # ✗ 每根都重算整段KDJ

conditions.* 里的函数是无状态纯函数，每次都在 bars[:i+1] 上重算指标，
放进 on_bar 就成了 O(n²)。实测差距（单股 5000 根 30分钟K）：

    指标放 prepare()  →  约 20 毫秒     ✓
    指标放 on_bar()   →  约 8500 毫秒   ✗ 全池验证会从 1 分钟变成 2 小时

所以本文件用 self._fast / self._slow 这种"预计算成序列、on_bar 只查下标"的写法。
门禁阈值见 config.USER_RULE_BUDGET_MS（默认 500 毫秒 / 5000 根）。

════════════════════════════════════════════════════════════════════
可以用的东西
════════════════════════════════════════════════════════════════════
app.indicators (ta)      ma / ema / macd / rsi / kdj / boll / cross_up / volume_ratio
app.patterns  (pt)       yang_bao_yin 阳包阴 / yin_bao_yang 阴包阳
app.backtest.conditions  时间窗、趋势分类、形态计数等（只在 prepare 里用，别在 on_bar 里用）

ctx 提供：ctx.bar() 当前K线、ctx.prev(n) 前n根、ctx.closes()/highs()/lows()
          ctx.i 当前下标、ctx.bars 全序列、ctx.position 当前持仓（卖出规则用）
          ctx.params 参数（就是下面 default_params 与界面上改过的值合并后的结果）

声明 default_params + PARAM_LABELS 后，验证台会自动生成参数表单。
再声明 PARAM_META 还能得到下拉/多选/时间控件，见 app/backtest/rules.py 里的例子。
"""
from typing import Optional

from app import indicators as ta
from app.backtest.rules import BuyRule, SellRule, register_buy, register_sell
from app.backtest.strategy import BarContext, Signal


@register_buy("example_ma_cross")
class ExampleMaCrossBuy(BuyRule):
    """快线上穿慢线买入，可选放量确认"""

    name = "示例：均线金叉"
    desc = "快线上穿慢线时买入；可要求当根成交量高于近期均量"
    default_params = {
        "buy_amount": 10000.0,      # 每笔固定买入金额（元）
        "fast": 5,                  # 快线周期
        "slow": 20,                 # 慢线周期
        "volume_ratio": 0.0,        # 量能倍数：当根量/前20根均量，0=关闭
    }
    PARAM_LABELS = {
        "buy_amount": "每笔买入金额(元)", "fast": "快线周期",
        "slow": "慢线周期", "volume_ratio": "量能倍数(0关)",
    }

    def reset(self) -> None:
        super().reset()
        self._fast = []
        self._slow = []

    def prepare(self, bars: list) -> None:
        # 指标在这里算一次，存成和 bars 等长的序列
        closes = [k["close"] for k in bars]
        self._fast = ta.ma(closes, int(self.params["fast"]))
        self._slow = ta.ma(closes, int(self.params["slow"]))

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        i = ctx.i
        if i < 1:
            return None
        # 只取相邻两点做金叉判断，O(1)
        f0, f1 = self._fast[i - 1], self._fast[i]
        s0, s1 = self._slow[i - 1], self._slow[i]
        if None in (f0, f1, s0, s1):
            return None                     # 均线预热期不足
        if not (f0 <= s0 and f1 > s1):
            return None                     # 未发生上穿

        need = float(self.params.get("volume_ratio") or 0)
        if need > 0:
            base = ctx.bars[max(0, i - 20):i]
            avg = sum(k["volume"] for k in base) / len(base) if base else 0.0
            if avg > 0 and ctx.bar()["volume"] / avg < need:
                return None                 # 量能不足
        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason=f"MA{self.params['fast']}上穿MA{self.params['slow']}")


@register_sell("example_ma_dead")
class ExampleMaDeadSell(SellRule):
    """快线下穿慢线卖出，固定止损兜底"""

    name = "示例：均线死叉"
    desc = "快线下穿慢线卖出；跌破固定止损优先离场"
    default_params = {
        "fast": 5,
        "slow": 20,
        "stop_loss_pct": 8.0,       # 固定止损%，0=关闭
    }
    PARAM_LABELS = {"fast": "快线周期", "slow": "慢线周期",
                    "stop_loss_pct": "固定止损%(0关)"}

    def reset(self) -> None:
        super().reset()
        self._fast = []
        self._slow = []

    def prepare(self, bars: list) -> None:
        closes = [k["close"] for k in bars]
        self._fast = ta.ma(closes, int(self.params["fast"]))
        self._slow = ta.ma(closes, int(self.params["slow"]))

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        # 卖出规则只在持仓中被调用，ctx.position 一定有值
        pos, bar, i = ctx.position, ctx.bar(), ctx.i
        # 1. 固定止损优先：用触发价成交而非收盘价，更接近真实
        sl_pct = float(self.params.get("stop_loss_pct") or 0)
        if sl_pct > 0:
            stop = pos.buy_price * (1 - sl_pct / 100.0)
            if bar["low"] <= stop:
                return Signal("sell", price=stop, reason=f"跌破{sl_pct}%止损")
        # 2. 均线死叉
        if i >= 1:
            f0, f1 = self._fast[i - 1], self._fast[i]
            s0, s1 = self._slow[i - 1], self._slow[i]
            if None not in (f0, f1, s0, s1) and f0 >= s0 and f1 < s1:
                return Signal("sell", reason=f"MA{self.params['fast']}下穿MA{self.params['slow']}")
        return None
