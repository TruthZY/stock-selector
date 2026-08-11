# -*- coding: utf-8 -*-
"""战法：KDJ+RSI 双金叉共振（30分钟K线范例）

买入条件（全部满足）：
  1. 近 golden_window 根 30m K线内，KDJ 金叉 与 RSI 金叉均出现过（共振）
  2. 当前K线时间 >= after（默认 11:00 后）
  3. 趋势为上涨后回调 / 横盘 / 下落企稳（排除连涨追高与下跌未企稳）
  4. 最近 pattern_days 个交易日内，阳包阴出现次数 > 阴包阳出现次数（且阳包阴 >= 1）
  5. 每笔固定买入 buy_amount 元（默认 1 万元），现金不足按剩余买入
  6. 该股当前无持仓（最大同时持仓数由引擎 max_positions 控制，默认 5）

卖出条件：
  1. 跌破 stop_loss_pct%（默认 10%）：盘中触及止损价即触发（触发价成交）
     —— 买入当日触发的止损受 T+1 限制顺延至下一交易日
  2. KDJ 死叉 或 RSI 死叉：出现即卖出；当日买入则按 T+1 规则次日卖出（引擎处理）

运行示例：
  python backtest.py --strategy kdj_rsi_golden --period 30m --scope codes \
      --codes 600519,000858 --start 2026-07-01 --max-positions 5 \
      --strategy-param buy_amount=10000,after=11:00,stop_loss_pct=10
"""
from app import indicators as ta
from app import patterns as pt
from app.backtest import conditions as cond
from app.backtest.position import Position
from app.backtest.strategy import BarContext, BaseStrategy, Signal, register


@register("kdj_rsi_golden")
class KdjRsiGoldenStrategy(BaseStrategy):
    key = "kdj_rsi_golden"
    name = "KDJ+RSI双金叉共振"
    desc = "近5根30mK线内KDJ与RSI双金叉、11点后、上涨回调/横盘/下落企稳、阳包阴占优时固定1万元买入；跌破10%或死叉卖出"
    default_params = {
        "buy_amount": 10000.0,      # 每笔固定买入金额（元）
        "kdj_n": 9,                 # KDJ 周期
        "rsi_fast": 6,              # RSI 快线周期
        "rsi_slow": 12,             # RSI 慢线周期
        "golden_window": 5,         # 双金叉判定窗口（K线根数）
        "after": "11:00",           # 买入时间条件：HH:MM 之后
        "pattern_days": 2,          # 阳包阴/阴包阳统计窗口（交易日数）
        "stop_loss_pct": 10.0,      # 止损跌幅 %
    }

    def reset(self) -> None:
        super().reset()
        self._k = self._d = []      # KDJ 序列（prepare 预计算）
        self._rf = self._rs = []    # RSI 快慢线序列
        self._bull = []             # 每根是否阳包阴（相对前一根）
        self._bear = []             # 每根是否阴包阳
        self._dates = []            # 每根所属交易日

    def prepare(self, bars: list) -> None:
        """预计算 KDJ/RSI 全序列与形态标记，on_bar 内 O(窗口) 查询"""
        highs = [k["high"] for k in bars]
        lows = [k["low"] for k in bars]
        closes = [k["close"] for k in bars]
        self._k, self._d, _ = ta.kdj(highs, lows, closes, self.params["kdj_n"])
        self._rf = ta.rsi(closes, self.params["rsi_fast"])
        self._rs = ta.rsi(closes, self.params["rsi_slow"])
        self._bull = [False] * len(bars)
        self._bear = [False] * len(bars)
        for j in range(1, len(bars)):
            self._bull[j] = pt.yang_bao_yin(bars[j - 1], bars[j])
            self._bear[j] = pt.yin_bao_yang(bars[j - 1], bars[j])
        self._dates = [k["ts"][:10] for k in bars]

    @staticmethod
    def _cross(a: list, b: list, j: int, up: bool) -> bool:
        """序列 a/b 在位置 j 是否上穿(up=True)/下穿"""
        if j < 1:
            return False
        if None in (a[j - 1], a[j], b[j - 1], b[j]):
            return False
        if up:
            return a[j - 1] <= b[j - 1] and a[j] > b[j]
        return a[j - 1] >= b[j - 1] and a[j] < b[j]

    def on_bar(self, ctx: BarContext) -> Signal | None:
        # ------------------------- 卖出（持仓中） -------------------------
        if ctx.position is not None:
            return self._sell_signal(ctx)

        # ------------------------- 买入（无持仓） -------------------------
        bar = ctx.bar()
        reasons = []

        # 1. 近 golden_window 根内 KDJ 与 RSI 双金叉均出现过
        w0 = max(0, ctx.i - self.params["golden_window"] + 1)
        kdj_golden = any(self._cross(self._k, self._d, j, up=True)
                         for j in range(w0, ctx.i + 1))
        rsi_golden = any(self._cross(self._rf, self._rs, j, up=True)
                         for j in range(w0, ctx.i + 1))
        if not (kdj_golden and rsi_golden):
            return None
        reasons.append(f"近{self.params['golden_window']}根双金叉")

        # 2. 时间条件：after 之后
        hit, r = cond.time_between(ctx.bars, ctx.i, after=str(self.params["after"]))
        if not hit:
            return None
        reasons.append(r)

        # 3. 趋势：上涨回调 / 横盘 / 下落企稳（排除连涨追高与下跌未企稳）
        trend = cond.classify_trend(ctx.bars, ctx.i)
        if trend not in ("up_pullback", "sideways", "down_stabilize"):
            return None
        reasons.append(f"趋势:{trend}")

        # 4. 最近 pattern_days 个交易日内阳包阴次数 > 阴包阳次数（且阳包阴 >= 1）
        day_set = set()
        for j in range(ctx.i, -1, -1):
            day_set.add(self._dates[j])
            if len(day_set) >= self.params["pattern_days"]:
                break
        bull = bear = 0
        for j in range(ctx.i, -1, -1):
            if self._dates[j] in day_set:
                bull += int(self._bull[j])
                bear += int(self._bear[j])
        if bull <= 0 or bull <= bear:
            return None
        reasons.append(f"阳包阴{bull}次>阴包阳{bear}次")

        # 5. 固定金额买入（持仓上限与无持仓检查由引擎保证）
        return Signal("buy", amount=float(self.params["buy_amount"]),
                      reason="；".join(reasons))

    def _sell_signal(self, ctx: BarContext) -> Signal | None:
        """卖出信号：止损优先于死叉；T+1（当日买入次日卖）由引擎顺延处理"""
        pos: Position = ctx.position
        bar = ctx.bar()
        # 1. 跌破止损：盘中触及止损价即触发（触发价成交）
        sl = pos.buy_price * (1 - self.params["stop_loss_pct"] / 100.0)
        if bar["low"] <= sl:
            return Signal("sell", price=sl,
                          reason=f"跌破{self.params['stop_loss_pct']}%止损")
        # 2. KDJ 死叉 或 RSI 死叉
        if self._cross(self._k, self._d, ctx.i, up=False):
            return Signal("sell", reason=f"KDJ死叉 @ {bar['ts']}")
        if self._cross(self._rf, self._rs, ctx.i, up=False):
            return Signal("sell", reason=f"RSI死叉 @ {bar['ts']}")
        return None
