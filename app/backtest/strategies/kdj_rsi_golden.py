# -*- coding: utf-8 -*-
"""预设战法：KDJ+RSI 双金叉共振（v2）—— 组合预设

= 买入规则: kdj_rsi_golden（双金叉共振，含低位/量能/涨幅/趋势/形态过滤）
  + 卖出规则: trailing_death_cross（固定止损→吊灯止损→死叉确认）

规则实现见 app/backtest/rules.py；本类仅做组装，便于 CLI/引擎按整包 key 使用。
也可在验证台/CLI 中自由组合其他买入+卖出规则：
  --buy-rule kdj_rsi_golden --sell-rule death_cross
"""
from app.backtest.rules import ComboStrategy
from app.backtest.strategy import register


@register("kdj_rsi_golden")
class KdjRsiGoldenStrategy(ComboStrategy):
    key = "kdj_rsi_golden"
    name = "KDJ+RSI双金叉共振(v2)"
    desc = "预设组合：双金叉共振买入（低位/量能/涨幅/趋势/形态过滤）+ 吊灯止损/死叉确认卖出"

    def __init__(self):
        super().__init__("kdj_rsi_golden", "trailing_death_cross")
        self.name = self.__class__.name
