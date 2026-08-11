# -*- coding: utf-8 -*-
"""回测系统：固定流程引擎 + 可扩展策略接口 + 绩效统计与报告"""
from app.backtest import conditions
from app.backtest.engine import BacktestConfig, BacktestEngine, run_backtest
from app.backtest.metrics import compute_metrics, build_report
from app.backtest.strategy import (
    BaseStrategy,
    BarContext,
    Signal,
    get_strategy,
    list_strategies,
    register,
)
