# -*- coding: utf-8 -*-
"""回测 CLI 入口：python backtest.py [options]（参数缺省取 config.BACKTEST_DEFAULT）

示例：
  python backtest.py --list-strategies
  python backtest.py --strategy all_in_all_out --period 30m --start 2025-01-01 --end 2026-08-01
  python backtest.py --scope watch --period daily --take-profit 15 --stop-loss 8
  python backtest.py --scope codes --codes 600519,000858 --period daily --strategy-param fast=5,slow=20
"""
import argparse
import asyncio
import sys

import config
from app.backtest import build_report, list_strategies
from app.backtest.engine import BacktestConfig, run_backtest

# Windows 终端默认 GBK 编码，统一输出为 UTF-8 避免中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _parse_params(s: str) -> dict:
    """--strategy-param fast=12,slow=26,signal=9 -> {"fast": 12, "slow": 26, "signal": 9}"""
    out = {}
    for item in s.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        try:
            out[k.strip()] = float(v) if ("." in v or "e" in v.lower()) else int(v)
        except ValueError:
            out[k.strip()] = v
    return out


def _print_summary(result: dict) -> None:
    m = result.get("metrics") or {}
    cfg = result.get("config") or {}
    strat = result.get("strategy") or {}
    print("=" * 56)
    print(f"战法: {strat.get('name') or cfg.get('strategy')} ({strat.get('key') or cfg.get('strategy')})")
    if result.get("ok"):
        print(f"标的: {len(result.get('stocks') or [])} 只 | 周期: {cfg.get('period')} | "
              f"区间: {cfg.get('start') or '数据起点'} ~ {cfg.get('end') or '数据终点'}")
        print(f"初始资金: {m.get('init_cash'):,.2f} → 期末: {m.get('final_value'):,.2f}")
        ds = result.get("data_source") or {}
        if ds:
            print(f"数据: 缓存命中 {ds.get('cache_hits', 0)} 只 / 在线拉取 {ds.get('fetched', 0)} 只")
        print(f"总收益率: {m.get('total_return_pct'):+.2f}%    "
              f"年化收益率: {m.get('annual_return_pct'):+.2f}%")
        print(f"胜率: {m.get('win_rate_pct')}% ({m.get('win_count')}/{m.get('trade_count')})    "
              f"盈亏比: {m.get('profit_loss_ratio') if m.get('profit_loss_ratio') is not None else '∞'}")
        dd_period = " ~ ".join(m.get("max_drawdown_period") or [])
        print(f"最大回撤: -{m.get('max_drawdown_pct')}% ({dd_period})")
        print(f"交易次数: {m.get('trade_count')}")
    else:
        print(f"回测失败: {result.get('msg')}")
    skipped = result.get("skipped") or []
    if skipped:
        show = "；".join(f"{s['code']}({s['reason']})" for s in skipped[:8])
        if len(skipped) > 8:
            show += f" 等{len(skipped)}只"
        print(f"跳过 {len(skipped)} 只: {show}")
    for w in result.get("warnings") or []:
        print(f"警告: {w}")
    print("=" * 56)


def main():
    parser = argparse.ArgumentParser(
        description="回测系统 CLI（参数缺省取 config.BACKTEST_DEFAULT）")
    parser.add_argument("--list-strategies", action="store_true", help="列出已注册战法")
    parser.add_argument("--strategy", help="整包战法 key（--buy-rule/--sell-rule 未指定时生效）")
    parser.add_argument("--buy-rule", help="买入规则 key（组合模式，如 kdj_rsi_golden/macd_golden/kdj_golden）")
    parser.add_argument("--sell-rule", help="卖出规则 key（组合模式，如 trailing_death_cross/death_cross/macd_death）")
    parser.add_argument("--scope", choices=["pool", "watch", "codes"], help="标的范围")
    parser.add_argument("--codes", help="scope=codes 时的代码列表，逗号分隔，如 600519,000858")
    parser.add_argument("--start", help="回测起点 YYYY-MM-DD")
    parser.add_argument("--end", help="回测终点 YYYY-MM-DD（空=数据最后一天）")
    parser.add_argument("--period", help="K线周期 1m/5m/15m/30m/60m/daily/weekly")
    parser.add_argument("--lookback", type=int, help="指标预热根数")
    parser.add_argument("--init-cash", type=float, help="初始资金")
    parser.add_argument("--commission-rate", type=float, help="手续费率（如 0.001=千一）")
    parser.add_argument("--slippage-rate", type=float, help="滑点率（如 0.002=千二）")
    parser.add_argument("--take-profit", type=float, help="止盈 %（0=关闭）")
    parser.add_argument("--stop-loss", type=float, help="止损 %（0=关闭）")
    parser.add_argument("--max-hold", type=int, help="持有期上限（K线根数，0=不限制）")
    parser.add_argument("--max-positions", type=int, help="最大同时持仓数（0=不限制）")
    parser.add_argument("--exec-price", choices=["close", "next_open"], help="成交价模式")
    parser.add_argument("--strategy-param", help="策略参数 k=v,k=v，如 fast=12,slow=26,signal=9")
    parser.add_argument("--no-report", action="store_true", help="只打印摘要，不生成报告")
    args = parser.parse_args()

    if args.list_strategies:
        from app.backtest.rules import list_rules
        print("已注册整包战法：")
        for s in list_strategies():
            print(f"  [{s['key']}] {s['name']} - {s['desc']}")
            if s["default_params"]:
                print(f"      默认参数: {s['default_params']}")
        rules = list_rules()
        print("\n买入规则（可与卖出规则自由组合）：")
        for r in rules["buy_rules"]:
            print(f"  [{r['key']}] {r['name']} - {r['desc']}")
        print("\n卖出规则：")
        for r in rules["sell_rules"]:
            print(f"  [{r['key']}] {r['name']} - {r['desc']}")
        return

    # 配置合并：config.BACKTEST_DEFAULT <- CLI 参数覆盖
    d = dict(config.BACKTEST_DEFAULT)
    for key, attr in [("strategy", "strategy"), ("scope", "scope"), ("start", "start"),
                      ("end", "end"), ("period", "period"), ("lookback", "lookback"),
                      ("init_cash", "init_cash"), ("commission_rate", "commission_rate"),
                      ("slippage_rate", "slippage_rate"), ("take_profit_pct", "take_profit"),
                      ("stop_loss_pct", "stop_loss"), ("max_hold_bars", "max_hold"),
                      ("max_positions", "max_positions"),
                      ("exec_price", "exec_price")]:
        v = getattr(args, attr)
        if v is not None:
            d[key] = v
    if args.codes:
        d["codes"] = [c.strip() for c in args.codes.split(",") if c.strip()]
    if args.strategy_param:
        d["params"] = _parse_params(args.strategy_param)
    if args.buy_rule and args.sell_rule:
        d["buy_rule"] = args.buy_rule
        d["sell_rule"] = args.sell_rule
        d["strategy"] = "combo"
    cfg = BacktestConfig.from_dict(d)

    result = asyncio.run(run_backtest(cfg))
    _print_summary(result)
    if result.get("ok") and not args.no_report:
        paths = build_report(result)
        print(f"HTML 报告: {paths['html']}")
        print(f"JSON 数据: {paths['json']}")


if __name__ == "__main__":
    main()
