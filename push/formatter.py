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


def _counts_tag(m: dict, window_days: int = 10) -> str:
    """近N个扫描日的盘前/盘后命中次数标注；未注入次数时返回空串（向后兼容）。"""
    cp = m.get("cnt_pre")
    cs = m.get("cnt_post")
    if cp is None and cs is None:
        return ""
    return f"近{window_days}日 盘前{cp or 0}·盘后{cs or 0}"


def build_error_section(errors=None, log_tail=None, max_errors: int = 8,
                        max_line: int = 160) -> List[str]:
    """构造"⚠️ 异常/错误"段（错误摘要 + 最近日志尾），无错无日志则返回空列表。

    errors   : 本次扫描采集的报错明细（个股取数/检测异常、快照批失败、超时等）
    log_tail : push.log 最近若干行（已脱敏）
    钉钉单条消息有长度上限，故错误只列前 max_errors 条、日志行截断到 max_line 字符。
    """
    errs = [e for e in (errors or []) if e]
    tail = [t for t in (log_tail or []) if t and t.strip()]
    if not errs and not tail:
        return []
    out: List[str] = ["", "### ⚠️ 异常/错误"]
    if errs:
        head = f"> 采集到 **{len(errs)}** 条报错"
        if len(errs) > max_errors:
            head += f"，仅列前 {max_errors} 条"
        out += [head, ""]
        for e in errs[:max_errors]:
            e = e if len(e) <= max_line else e[:max_line] + "…"
            out.append(f"- `{e}`")
    if tail:
        out += ["", f"> 最近 {len(tail)} 行日志：", "", "```"]
        for t in tail:
            out.append(t if len(t) <= max_line else t[:max_line] + "…")
        out.append("```")
    return out


def build_message(result, scan_time: str = "14:00", top_n: int = 0,
                  window_days: int = 10, log_tail=None) -> Tuple[str, str]:
    """把 ScanResult 合并成 (title, markdown)。top_n>0 时只取前 N 只。
    result.errors 非空或传入 log_tail 时，末尾追加"⚠️ 异常/错误"段。"""
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
            cnt_s = _counts_tag(m, window_days)
            cnt_part = f" `{cnt_s}`" if cnt_s else ""
            lines.append(
                f"**{idx}. {m.get('name','')}（{m.get('code','')}）** "
                f"现价 {price_s} `{chg_s}` `{ang_s}`{cnt_part}")
            lines.append(f"　{m.get('reason','')}")
            lines.append("")
    lines.append("---")
    lines.append("_盘中预估，尾盘可能变化，仅供参考，不构成投资建议_")
    lines += build_error_section(getattr(result, "errors", None), log_tail)
    return title, "\n".join(lines)


def build_postclose_message(result, scan_time: str = "21:00", top_n: int = 0,
                            window_days: int = 10, log_tail=None) -> Tuple[str, str]:
    """盘后推送（精简版）：只列 排名 + 名称 + 代码 + 近N日盘前/盘后命中次数。
    不含现价/涨跌幅/中轨角度/建仓信号明细（按需求"不推送详细数据"）。"""
    plabel = period_label(result.period)
    matches: List[dict] = list(result.matches)
    total = len(matches)
    if top_n > 0:
        matches = matches[:top_n]
    shown = len(matches)

    title = f"建仓信号推送 · {plabel} · 盘后"
    count_s = (f"命中 **{total}** 只，展示前 {shown} 只"
               if shown < total else f"命中 **{shown}** 只")
    lines = [
        f"### 🌙 建仓信号推送 · {plabel} · 盘后收盘确认 {scan_time}",
        "",
        f"> {count_s} / 参与判定 {result.with_data} 只 · {result.date}",
        "",
    ]
    if not matches:
        lines.append("_今日盘后无命中信号_")
    else:
        for idx, m in enumerate(matches, 1):
            cnt_s = _counts_tag(m, window_days)
            cnt_part = f"　`{cnt_s}`" if cnt_s else ""
            lines.append(
                f"**{idx}. {m.get('name','')}（{m.get('code','')}）**{cnt_part}")
            # 钉钉 markdown 会把无空行的连续单行合并成一段渲染，
            # 每条之间必须空一行才能"一排一排"分开显示（与盘前 build_message 一致）
            lines.append("")
    lines.append("---")
    lines.append(f"_盘后收盘数据；次数=近{window_days}个扫描日命中回数，仅供参考，不构成投资建议_")
    lines += build_error_section(getattr(result, "errors", None), log_tail)
    return title, "\n".join(lines)


def build_degraded_message(result, scan_time: str = "14:00", log_tail=None) -> Tuple[str, str]:
    """覆盖率过低时的降级告警（避免把"没扫到"误报成"无信号"）。"""
    plabel = period_label(result.period)
    title = f"数据降级告警 · {plabel}"
    lines = [
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
    ]
    lines += build_error_section(getattr(result, "errors", None), log_tail)
    return title, "\n".join(lines)
