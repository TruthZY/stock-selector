# -*- coding: utf-8 -*-
"""消息格式化：把一个周期的命中清单合并成一条钉钉 markdown。

P2 为基础版（够跑通链路 + 肉眼验收），P4 再精修排序/字段/多周期分组。
注意：钉钉「自定义关键词」模式下正文必须含关键词，标题统一用"…推送"自然带词；
即便忘带，DingTalkPusher._ensure_keyword 也会兜底补进正文。
"""
from __future__ import annotations

from typing import List, Tuple

_PERIOD_LABEL = {"daily": "日K", "30m": "30分钟K", "60m": "60分钟K", "15m": "15分钟K"}


def period_label(period: str) -> str:
    return _PERIOD_LABEL.get(period, period)


def _mode_label(mode: str, scan_time: str) -> str:
    if mode == "close":
        return "收盘确认"
    return f"盘中预估 {scan_time}（未收盘）"


def build_message(result, scan_time: str = "14:00", top_n: int = 0) -> Tuple[str, str]:
    """把 ScanResult 合并成 (title, markdown)。top_n>0 时只取前 N 只。"""
    plabel = period_label(result.period)
    mlabel = _mode_label("live" if result.period == "daily" else "close", scan_time)
    matches: List[dict] = list(result.matches)
    total = len(matches)
    if top_n > 0:
        matches = matches[:top_n]
    shown = len(matches)

    title = f"建仓信号推送 · {plabel}"
    count_s = (f"命中 **{total}** 只，展示强度前 {shown} 只"
               if shown < total else f"命中 **{shown}** 只")
    lines = [
        f"### 📈 建仓信号推送 · {plabel} · {mlabel}",
        "",
        f"> {count_s} / 参与判定 {result.with_data} 只"
        f" / 计划 {result.scanned} 只 · 覆盖率 {result.coverage*100:.0f}%"
        f" · {result.date}",
        "",
    ]
    if not matches:
        lines.append("_今日该周期无命中信号_")
    else:
        for idx, m in enumerate(matches, 1):
            chg = m.get("change_pct")
            chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
            price = m.get("price")
            price_s = f"{price:.2f}" if isinstance(price, (int, float)) else "—"
            ang = m.get("mid_angle")
            ang_s = f"中轨{ang:+.1f}°" if isinstance(ang, (int, float)) else "中轨—"
            lines.append(
                f"**{idx}. {m.get('name','')}（{m.get('code','')}）** "
                f"现价 {price_s} `{chg_s}` `{ang_s}`")
            lines.append(f"　{m.get('reason','')}")
            lines.append("")
    lines.append("---")
    lines.append("_盘中预估，尾盘可能变化，仅供参考，不构成投资建议_")
    return title, "\n".join(lines)


def build_degraded_message(result, scan_time: str = "14:00") -> Tuple[str, str]:
    """覆盖率过低时的降级告警（避免把"没扫到"误报成"无信号"）。"""
    plabel = period_label(result.period)
    title = f"数据降级告警 · {plabel}"
    md = "\n".join([
        f"### ⚠️ 数据降级告警 · {plabel} · {scan_time}",
        "",
        f"> 覆盖率仅 **{result.coverage*100:.0f}%**（{result.with_data}/{result.scanned}），"
        f"低于阈值，本次推送不可信。",
        "",
        f"可能原因：数据源限流/超时、快照大面积缺失。跳过 {result.skipped} 只。",
        f"耗时 {result.elapsed_ms}ms · {result.date}",
        "",
        "---",
        "_推送系统自动告警，请检查数据源与网络_",
    ])
    return title, md
