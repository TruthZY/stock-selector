# -*- coding: utf-8 -*-
"""回测持仓与交易记录数据结构"""
from dataclasses import dataclass, field


@dataclass
class Position:
    """盘中持仓状态（买入成交后创建，平仓后转为 Trade）"""
    code: str
    name: str
    buy_ts: str
    buy_price: float            # 实际买入价（含滑点）
    shares: float               # 股数（简化模型，不做 100 股整手约束）
    cost: float                 # 总成本 = 买入金额 + 手续费
    entry_bar_index: int        # 入场K线在 bars 中的索引（用于持有期计算）
    buy_reason: str = ""        # 买入原因


@dataclass
class Trade:
    """已平仓交易记录（用于交易明细与统计）"""
    code: str
    name: str
    buy_ts: str
    buy_price: float            # 实际买入价（含滑点）
    sell_ts: str
    sell_price: float           # 实际卖出价（含滑点）
    shares: float
    pnl: float                  # 净盈亏（含双向手续费与滑点）
    pnl_pct: float              # 盈亏比例 %（相对总成本）
    hold_bars: int              # 持有K线根数
    buy_reason: str = ""
    sell_reason: str = ""
