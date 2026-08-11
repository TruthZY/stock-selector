# -*- coding: utf-8 -*-
"""示例战法：MACD 金叉全仓买入 / 死叉全仓卖出（30分钟K线范例，参数可调）

- 买入：DIF 上穿 DEA（金叉），且当前无持仓 → 全仓买入
- 卖出：DIF 下穿 DEA（死叉），且当前有持仓 → 全部卖出
- 参数：fast/slow/signal 为 MACD 周期，可通过 CLI --strategy-param 覆盖
- 示例：python backtest.py --strategy all_in_all_out --period 30m \
        --strategy-param fast=12,slow=26,signal=9
"""
from app import indicators as ta
from app.backtest.position import Position
from app.backtest.strategy import BarContext, BaseStrategy, Signal, register


@register("all_in_all_out")
class AllInAllOutStrategy(BaseStrategy):
    key = "all_in_all_out"
    name = "MACD金叉全仓进出"
    desc = "MACD DIF上穿DEA金叉全仓买入，死叉全仓卖出（30分钟K线范例）"
    default_params = {"fast": 12, "slow": 26, "signal": 9}

    def reset(self) -> None:
        super().reset()
        self._dif: list = []    # 全序列 DIF（prepare 预计算）
        self._dea: list = []

    def prepare(self, bars: list) -> None:
        """一次性预计算整段K线的 MACD，on_bar 中 O(1) 查当前与前一值"""
        closes = [k["close"] for k in bars]
        self._dif, self._dea, _ = ta.macd(
            closes, self.params["fast"], self.params["slow"], self.params["signal"])

    def on_bar(self, ctx: BarContext) -> Signal | None:
        if ctx.i < 1:
            return None
        dif0, dea0 = self._dif[ctx.i - 1], self._dea[ctx.i - 1]
        dif1, dea1 = self._dif[ctx.i], self._dea[ctx.i]
        if None in (dif0, dea0, dif1, dea1):
            return None
        ts = ctx.bar()["ts"]
        if ctx.position is None and dif0 <= dea0 and dif1 > dea1:
            # 金叉：前值 DIF 不高于 DEA，当前上穿 → 全仓买入
            return Signal("buy", reason=f"MACD金叉 @ {ts} (DIF{dif1:.3f}上穿DEA{dea1:.3f})")
        if ctx.position is not None and dif0 >= dea0 and dif1 < dea1:
            # 死叉：前值 DIF 不低于 DEA，当前下穿 → 全部卖出
            return Signal("sell", reason=f"MACD死叉 @ {ts} (DIF{dif1:.3f}下穿DEA{dea1:.3f})")
        return None
