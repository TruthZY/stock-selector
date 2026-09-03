# -*- coding: utf-8 -*-
"""建仓检测战法：越千山建仓指标方法论

买入：accumulation_detect（BOLL通道+量价+低位+低点抬升 四维共振）
卖出：accumulation_exit（BOLL下轨拐头+固定止损+MACD死叉）

适用周期：日线（原始指标声明日线和周线有效）
示例：python backtest.py --strategy accumulation --period daily
"""
from app.backtest.rules import ComboStrategy
from app.backtest.strategy import register


@register("accumulation")
class AccumulationStrategy(ComboStrategy):
    key = "accumulation"
    name = "越千山建仓检测"
    desc = ("基于越千山建仓指标方法论，检测主力建仓行为。\n"
            "买入：BOLL下轨上移+低点抬高+低位+量价结构 四维共振\n"
            "卖出：BOLL下轨拐头+固定止损+MACD死叉")

    def __init__(self):
        super().__init__("accumulation_detect", "accumulation_exit")
        self.name = self.__class__.name
        self.desc = self.__class__.desc
