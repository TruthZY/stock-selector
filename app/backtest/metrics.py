# -*- coding: utf-8 -*-
"""回测绩效统计与报告生成：指标计算 + JSON 数据 + HTML(ECharts) 资金曲线报告"""
import json
import os
import time
from datetime import datetime
from typing import Dict, List

from app.backtest.position import Trade


def _parse_ts(ts: str) -> datetime:
    """解析 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD' 时间戳"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return datetime.strptime(ts[:10], "%Y-%m-%d")


def compute_metrics(equity_curve: List[dict], trades: List[Trade], init_cash: float) -> dict:
    """计算总收益/年化/胜率/盈亏比/最大回撤等指标"""
    if not equity_curve:
        return {}
    final = equity_curve[-1]["value"]
    total_return_pct = (final / init_cash - 1) * 100.0 if init_cash else 0.0

    # 年化收益率：按首末净值时间差（自然日）折算
    annual_return_pct, period_days = 0.0, 0
    try:
        period_days = (_parse_ts(equity_curve[-1]["ts"]) - _parse_ts(equity_curve[0]["ts"])).days + 1
        if period_days > 0 and final > 0 and init_cash > 0:
            annual_return_pct = ((final / init_cash) ** (365.0 / period_days) - 1) * 100.0
    except ValueError:
        pass

    # 胜率与盈亏比（按每笔净盈亏金额）
    total = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate_pct = len(wins) / total * 100.0 if total else 0.0
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 0.0
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else (float("inf") if wins else 0.0)

    # 最大回撤及时间段（资金曲线峰值之后的最大跌幅）
    peak, max_dd = -float("inf"), 0.0
    peak_ts = trough_ts = dd_start_ts = equity_curve[0]["ts"]
    for pt in equity_curve:
        if pt["value"] > peak:
            peak = pt["value"]
            dd_start_ts = pt["ts"]
        dd = 1.0 - pt["value"] / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            peak_ts = dd_start_ts
            trough_ts = pt["ts"]

    return {
        "init_cash": round(init_cash, 2),
        "final_value": round(final, 2),
        "total_return_pct": round(total_return_pct, 2),
        "annual_return_pct": round(annual_return_pct, 2),
        "period_days": period_days,
        "trade_count": total,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(win_rate_pct, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio != float("inf") else None,
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "max_drawdown_period": [peak_ts, trough_ts],
    }


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>回测报告 - {strategy_name}</title>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --border:#21262d; --text:#e6edf3; --muted:#8b949e;
          --accent:#58a6ff; --up:#f85149; --down:#3fb950; --warn:#d29922; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font:13px/1.6 "Microsoft YaHei",sans-serif; padding:20px; }}
  h1 {{ font-size:18px; margin-bottom:4px; }}
  .sub {{ color:var(--muted); font-size:12px; margin-bottom:16px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin-bottom:20px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px 14px; }}
  .card .label {{ color:var(--muted); font-size:12px; }}
  .card .value {{ font-size:20px; font-weight:600; margin-top:4px; }}
  .card .hint {{ color:var(--muted); font-size:11px; margin-top:2px; }}
  .pos {{ color:var(--up); }} .neg {{ color:var(--down); }}
  .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px; margin-bottom:20px; }}
  .panel h2 {{ font-size:14px; margin-bottom:10px; }}
  #chart {{ width:100%; height:420px; }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  th,td {{ padding:6px 8px; border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; }}
  th {{ color:var(--muted); font-weight:normal; position:sticky; top:0; background:var(--panel); }}
  td:first-child, th:first-child, td:nth-child(2), th:nth-child(2) {{ text-align:left; }}
  .tbl-wrap {{ max-height:480px; overflow-y:auto; }}
  .muted {{ color:var(--muted); }} .warn {{ color:var(--warn); }}
  ul {{ margin-left:18px; color:var(--muted); }}
</style>
</head>
<body>
<h1>📊 回测报告：{strategy_name}</h1>
<div class="sub" id="subTitle">--</div>
<div class="cards" id="cards"></div>
<div class="panel"><h2>资金曲线（含最大回撤区间标注）</h2><div id="chart"></div></div>
<div class="panel"><h2>交易明细（共 {trade_count} 笔）</h2>
  <div class="tbl-wrap"><table id="tbl">
    <thead><tr><th>代码</th><th>名称</th><th>买入时间</th><th>买入价</th><th>卖出时间</th>
      <th>卖出价</th><th>股数</th><th>盈亏额</th><th>盈亏%</th><th>持有根数</th><th>买入原因</th><th>卖出原因</th></tr></thead>
    <tbody></tbody>
  </table></div>
</div>
<div class="panel"><h2>配置与假设</h2>
  <ul id="cfgList"></ul>
</div>
<script src="../static/echarts.min.js"></script>
<script>
const RESULT = {result_json};
const M = RESULT.metrics || {{}};
const fmt = (v, d=2) => v == null ? '-' : Number(v).toLocaleString('zh-CN', {{minimumFractionDigits:d, maximumFractionDigits:d}});
const pct = (v, d=2) => v == null ? '-' : (v>0?'+':'') + Number(v).toFixed(d) + '%';

// 指标卡片
const cards = [
  {{label:'总收益率', value:pct(M.total_return_pct), cls:M.total_return_pct>=0?'pos':'neg', hint:'期末 '+fmt(M.final_value)}},
  {{label:'年化收益率', value:pct(M.annual_return_pct), cls:M.annual_return_pct>=0?'pos':'neg', hint: M.period_days+' 天'}},
  {{label:'胜率', value: M.win_rate_pct==null?'-':fmt(M.win_rate_pct)+'%', hint: M.win_count+'胜 / '+M.loss_count+'负 / 共'+M.trade_count+'笔'}},
  {{label:'盈亏比', value: M.profit_loss_ratio==null?'-':fmt(M.profit_loss_ratio), hint:'平均盈 '+fmt(M.avg_win)+' / 均亏 '+fmt(M.avg_loss)}},
  {{label:'最大回撤', value: M.max_drawdown_pct==null?'-':'-'+fmt(M.max_drawdown_pct)+'%', cls:'neg',
     hint: (M.max_drawdown_period||[]).join(' ~ ')}},
  {{label:'初始资金', value: fmt(M.init_cash)}},
];
document.getElementById('cards').innerHTML = cards.map(c =>
  `<div class="card"><div class="label">${{c.label}}</div><div class="value ${{c.cls||''}}">${{c.value}}</div><div class="hint">${{c.hint||''}}</div></div>`
).join('');
document.getElementById('subTitle').textContent =
  `战法 ${{RESULT.strategy.name||RESULT.strategy.key}} | 标的 ${{RESULT.stocks.length}} 只 | 周期 ${{RESULT.config.period}} | 区间 ${{RESULT.config.start||'数据起点'}} ~ ${{RESULT.config.end||'数据终点'}}`;

// 资金曲线
const curve = RESULT.equity_curve || [];
const chart = echarts.init(document.getElementById('chart'));
chart.setOption({{
  backgroundColor:'transparent',
  tooltip: {{trigger:'axis'}},
  grid: {{left:60, right:20, top:30, bottom:40}},
  xAxis: {{type:'category', data:curve.map(p=>p.ts), axisLabel:{{color:'#8b949e'}}}},
  yAxis: {{type:'value', scale:true, axisLabel:{{color:'#8b949e', formatter:v=>fmt(v,0)}}}},
  dataZoom: [{{type:'inside'}}, {{type:'slider', height:18}}],
  series: [{{
    name:'组合净值', type:'line', showSymbol:false, data:curve.map(p=>p.value),
    lineStyle:{{color:'#58a6ff', width:1.5}}, areaStyle:{{color:'rgba(88,166,255,.12)'}},
    markArea: M.max_drawdown_period ? {{
      silent:true, itemStyle:{{color:'rgba(248,81,73,.10)'}},
      data: [[{{xAxis: M.max_drawdown_period[0]}}, {{xAxis: M.max_drawdown_period[1]}}]],
    }} : undefined,
    markPoint: {{
      data: [{{type:'max', name:'峰值', symbolSize:42}}, {{type:'min', name:'谷值', symbolSize:42}}],
    }},
  }}],
}});
window.addEventListener('resize', () => chart.resize());

// 交易明细
const tb = document.querySelector('#tbl tbody');
tb.innerHTML = (RESULT.trades||[]).map(t => `<tr>
  <td>${{t.code}}</td><td>${{t.name}}</td><td>${{t.buy_ts}}</td><td>${{fmt(t.buy_price)}}</td>
  <td>${{t.sell_ts}}</td><td>${{fmt(t.sell_price)}}</td><td>${{fmt(t.shares,0)}}</td>
  <td class="${{t.pnl>=0?'pos':'neg'}}">${{fmt(t.pnl)}}</td>
  <td class="${{t.pnl>=0?'pos':'neg'}}">${{pct(t.pnl_pct)}}</td><td>${{t.hold_bars}}</td>
  <td>${{t.buy_reason}}</td><td>${{t.sell_reason}}</td>
</tr>`).join('');

// 配置与假设
const cfg = RESULT.config || {{}};
const items = [
  ['标的范围', cfg.scope + (cfg.scope==='codes' ? ' ['+(cfg.codes||[]).join(',')+']' : '')],
  ['回测区间', (cfg.start||'数据起点') + ' ~ ' + (cfg.end||'数据终点')],
  ['K线周期', cfg.period], ['初始资金', fmt(cfg.init_cash)],
  ['手续费', (cfg.commission_rate*100).toFixed(1)+'‰'], ['滑点', (cfg.slippage_rate*100).toFixed(1)+'‰'],
  ['止盈/止损/持有期', (cfg.take_profit_pct||0)+'% / '+(cfg.stop_loss_pct||0)+'% / '+(cfg.max_hold_bars||0)+'根'],
  ['成交价模式', cfg.exec_price],
];
document.getElementById('cfgList').innerHTML =
  items.map(([k,v]) => `<li><b>${{k}}</b>：${{v}}</li>`).join('') +
  (RESULT.assumptions||[]).map(a => `<li class="muted">${{a}}</li>`).join('') +
  ((RESULT.skipped||[]).length ? `<li class="warn">跳过 ${{RESULT.skipped.length}} 只：${{RESULT.skipped.map(s=>s.code+'('+s.reason+')').join('；')}}</li>` : '');
</script>
</body>
</html>
"""


def build_report(result: dict, save_dir: str = "reports") -> Dict[str, str]:
    """生成 JSON 数据文件与 HTML 资金曲线报告，返回 {"json": path, "html": path}"""
    os.makedirs(save_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.join(save_dir, f"backtest_{stamp}")
    json_path, html_path = base + ".json", base + ".html"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    result_json = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    html = _HTML_TEMPLATE.format(
        strategy_name=(result.get("strategy") or {}).get("name") or "",
        result_json=result_json,
        trade_count=len(result.get("trades") or []),
    )
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return {"json": json_path, "html": html_path}
