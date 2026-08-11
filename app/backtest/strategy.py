# -*- coding: utf-8 -*-
"""回测策略扩展接口：新增战法 = 继承 BaseStrategy + @register("key")，无需改动引擎

策略职责：
- on_bar：每根K线调用一次，根据当前行情与持仓状态返回买入/卖出信号（Signal）或 None
- 卖出条件（止盈/止损/持有期）由引擎级风控统一处理（配置项），策略也可自行发卖出信号
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Type

from app.backtest.position import Position


@dataclass
class Signal:
    """交易信号"""
    action: str                    # "buy" / "sell"
    reason: str = ""               # 触发原因说明（写入交易明细）
    fraction: float = 1.0          # 买入使用可用资金比例（1.0=全仓）；amount 指定时忽略
    amount: Optional[float] = None # 可选：买入固定金额（如每笔 1 万元），优先于 fraction；现金不足按剩余买入
    price: Optional[float] = None  # 可选：策略指定成交价；None 则按配置 exec_price 模式


class BarContext:
    """单只股票的单根K线上下文（K线为升序，含预热期数据）"""

    def __init__(self, code: str, name: str, bars: List[dict], i: int,
                 position: Optional[Position], params: Dict):
        self.code = code
        self.name = name
        self.bars = bars                # 升序K线（预热期 + 回测区间）
        self.i = i                      # 当前K线在 bars 中的索引
        self.position = position        # 当前持仓（无则 None）
        self.params = params            # 策略参数

    # ------------------------- 便捷方法 -------------------------

    def bar(self) -> dict:
        """当前K线 {ts, open, close, high, low, volume, amount}"""
        return self.bars[self.i]

    def prev(self, n: int = 1) -> Optional[dict]:
        """前第 n 根K线（超出范围返回 None）"""
        j = self.i - n
        return self.bars[j] if j >= 0 else None

    def closes(self, count: int = 0) -> List[float]:
        """收盘价序列（count=0 返回全部，否则返回最近 count 根）"""
        seq = [k["close"] for k in self.bars[:self.i + 1]]
        return seq if count <= 0 else seq[-count:]

    def highs(self, count: int = 0) -> List[float]:
        seq = [k["high"] for k in self.bars[:self.i + 1]]
        return seq if count <= 0 else seq[-count:]

    def lows(self, count: int = 0) -> List[float]:
        seq = [k["low"] for k in self.bars[:self.i + 1]]
        return seq if count <= 0 else seq[-count:]

    def volumes(self, count: int = 0) -> List[float]:
        seq = [k["volume"] for k in self.bars[:self.i + 1]]
        return seq if count <= 0 else seq[-count:]


class BaseStrategy(ABC):
    """战法基类：key 唯一，default_params 为策略参数默认值（CLI --strategy-param 可覆盖）"""

    key: str = ""
    name: str = ""
    desc: str = ""
    default_params: Dict = {}

    def __init__(self):
        self.params: Dict = dict(self.default_params or {})

    def reset(self) -> None:
        """每只股票回测前调用，用于重置策略内部状态（如入场标记）"""
        self.params = dict(self.default_params or {})

    def prepare(self, bars: List[dict]) -> None:
        """可选钩子：回测开始前对整段K线（含预热）预计算指标序列并缓存，
        避免 on_bar 中反复全量计算（如对每根K线重算 MACD）"""

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        """每根K线回调，返回交易信号或 None"""

    def on_position_opened(self, ctx: BarContext, position: Position) -> None:
        """可选钩子：买入成交后调用"""

    def on_position_closed(self, ctx: BarContext, position: Position) -> None:
        """可选钩子：卖出成交后调用"""


# ---------------------------------------------------------------------------
# 注册机制
# ---------------------------------------------------------------------------

STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {}


def register(key: str):
    """战法注册装饰器：@register("my_war")"""
    def deco(cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
        cls.key = key
        STRATEGY_REGISTRY[key] = cls
        return cls
    return deco


def get_strategy(key: str) -> BaseStrategy:
    """按 key 实例化战法，未知战法抛 KeyError"""
    if key not in STRATEGY_REGISTRY:
        raise KeyError(f"未知战法: {key}，可用: {', '.join(STRATEGY_REGISTRY) or '无'}")
    return STRATEGY_REGISTRY[key]()


def list_strategies() -> List[dict]:
    """列出全部已注册战法（key/name/desc/默认参数）"""
    return [{"key": cls.key, "name": cls.name, "desc": cls.desc,
             "default_params": dict(cls.default_params or {})}
            for cls in STRATEGY_REGISTRY.values()]


# 导入内置战法以完成注册（放在注册函数之后避免循环依赖）
from app.backtest import strategies  # noqa: E402,F401
