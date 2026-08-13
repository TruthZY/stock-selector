# -*- coding: utf-8 -*-
"""K线时间语义工具：判断某根K线是否已收盘

数据源会返回**正在形成**的当前K线：实测 11:16 时 30m 末根 ts=11:30
（覆盖 11:00-11:30），成交量只有已收盘那根的三成。

ts 是K线的**结束**时刻，所以 ts <= 现在 即该根已收盘。日线/周线的 ts 只有
日期，当日那根要等到收盘（15:00）之后才算最终。

为什么必须区分：盘中评估未收盘K线，指标会在一根K线内反复翻转。实测全池 121 只
60m 历史，用"半根"判断 MACD 金叉会报出 917 条，其中 146 条（15.9%）到收盘就
不成立了；同时还漏掉 214 条收盘后才成立的。改成只看已收盘K线是 985 条全真。

放在独立模块而不是 app/strategies.py：app/rule_signals.py 在模块顶层
import strategies，strategies 再反向 import rule_signals 就成环了。
"""
from datetime import datetime
from typing import List, Optional

# A股收盘时刻（分钟数）：日线/周线当日那根要过了这个点才算最终
_DAILY_CLOSE_MINUTE = 15 * 60


def is_closed(ts: str, now: Optional[datetime] = None) -> bool:
    """该K线是否已收盘（ts 为K线结束时刻）"""
    now = now or datetime.now()
    if len(ts) <= 10:                       # 日线/周线：只有日期
        if ts[:10] < now.strftime("%Y-%m-%d"):
            return True
        return now.hour * 60 + now.minute >= _DAILY_CLOSE_MINUTE
    return ts[:16] <= now.strftime("%Y-%m-%d %H:%M")


def last_closed_index(bars: List[dict], now: Optional[datetime] = None) -> Optional[int]:
    """最后一根已收盘K线的下标；没有则 None。从末尾往前找，通常一步命中"""
    now = now or datetime.now()
    for i in range(len(bars) - 1, -1, -1):
        if is_closed(bars[i]["ts"], now):
            return i
    return None
