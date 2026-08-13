# -*- coding: utf-8 -*-
"""规则筛选：把买入规则（BuyRule）应用到指定交易日的K线上，选出触发规则的股票

实时信号是"在固定池子上持续侦测、去重后入库"；这里是一次性查询——给定
规则/周期/日期/范围，对范围内每只股票评估"该日规则是否触发"，返回命中清单。
盘中查今天 = 盘中异动语义；盘后查当天 = 收盘复盘。

mode=live（盘中异动）：评估该日最后一根K线（含进行中），与实时盘中语义一致；
mode=close（买入战法）：评估该日最后一根**已收盘**K线，与回测逐根一致。
"""
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.backtest.rules import BUY_REGISTRY, _ensure_user_rules
from app.backtest.strategy import BarContext
from app.bars import is_closed

# 每只股票拉取的K线根数：30m 约 37 个交易日，够各内置规则的指标预热
SCAN_KLINE_LIMIT = 300
SCAN_CONCURRENCY = 10


def _day_range(bars: List[dict], day_ts: str) -> Optional[Tuple[int, int]]:
    """该交易日的K线在 bars 中的闭区间 [start, end]；无则 None（当日停牌等）"""
    start = end = None
    for i, k in enumerate(bars):
        if k["ts"][:10] == day_ts:
            if start is None:
                start = i
            end = i
    return (start, end) if start is not None else None


def _eval_on_day(code: str, rule_cls, bars: List[dict], day_ts: str,
                 confirm_on_close: bool) -> Tuple[bool, str, str]:
    """在指定交易日的K线上评估规则，返回 (命中, reason, 评估K线ts)

    confirm_on_close=True 时盘中当日末根可能未收盘，向前找该日最后一根已收盘K线
    （与实时"买入战法"语义一致）。规则异常视为未命中：单只股票的脏数据或
    规则 bug 不打断整场扫描。
    """
    rng = _day_range(bars, day_ts)
    if rng is None:
        return False, "", ""
    start, end = rng
    i = end
    if confirm_on_close:
        while i >= start and not is_closed(bars[i]["ts"]):
            i -= 1
        if i < start:
            return False, "", ""     # 该日还没有已收盘K线（如刚开盘）
    try:
        rule = rule_cls()
        rule.reset()
        rule.prepare(bars)
        sig = rule.on_bar(BarContext(code, "", bars, i, None, rule.params))
    except Exception:
        return False, "", ""
    if sig is None or getattr(sig, "action", "") != "buy":
        return False, "", ""
    return True, sig.reason or "", bars[i]["ts"]


def _resolve_day(data: Dict[str, List[dict]], day: str) -> Optional[str]:
    """解析目标交易日：today=今天 / latest=数据最后一天 / prev=倒数第二天"""
    all_dates = sorted({k["ts"][:10] for kl in data.values() for k in kl})
    if not all_dates:
        return None
    if day == "today":
        today = datetime.now().strftime("%Y-%m-%d")
        return today if today in all_dates else None
    if day == "prev":
        return all_dates[-2] if len(all_dates) >= 2 else all_dates[-1]
    return all_dates[-1]


def _resolve_scope(store, scope: str, group_id: Optional[int] = None) -> List[Tuple[str, str]]:
    if scope == "watch":
        return store.get_watch()
    if scope == "group":
        return store.get_group_stocks(group_id) if group_id is not None else []
    return store.get_stocks()


async def run_rule_scan(rule: str, period: str, scope: str, day: str,
                        mode: str, store, ds, scanner=None,
                        group_id: Optional[int] = None) -> dict:
    """按规则扫描指定交易日，返回命中清单（前端战法筛选面板使用）

    rule:   买入规则 key（含 user_rules/ 里自定义的）
    period: K线周期 30m/60m/15m/5m/daily
    scope:  pool=股票池 / watch=自选 / group=自定义分组（需 group_id）
    day:    today=今天 / latest=最近交易日 / prev=前一天
    mode:   live=盘中异动（判定末根）/ close=买入战法（判定已收盘末根）
    """
    start = time.time()
    _ensure_user_rules()        # 让 user_rules/ 里的规则也进 BUY_REGISTRY
    cls = BUY_REGISTRY.get(rule)
    if cls is None:
        return {"ok": False, "msg": f"未知买入规则 {rule!r}，可用："
                                     f"{', '.join(sorted(BUY_REGISTRY)) or '无'}"}
    confirm_on_close = mode == "close"
    stocks = _resolve_scope(store, scope, group_id)
    if not stocks:
        return {"ok": False, "msg": "该范围内没有股票，请先在对应列表里添加"}
    codes = [c for c, _ in stocks]
    names = dict(stocks)

    data = await ds.fetch_many_kline(
        codes, period, SCAN_KLINE_LIMIT, concurrency=SCAN_CONCURRENCY)
    target = _resolve_day(data, day)
    result = {"ok": True, "rule": rule, "rule_name": cls.name or rule,
              "period": period, "mode": mode, "scope": scope,
              "group_id": group_id,
              "date": target, "matches": [], "scanned": len(codes),
              "fetched": len(data), "elapsed_ms": 0}
    if target is None:
        result["msg"] = "该日期尚无K线数据（今天尚未产生K线或数据源未更新）"
        result["elapsed_ms"] = int((time.time() - start) * 1000)
        return result

    # 命中股票的实时快照（价格/涨跌幅）。监控范围内的股票快照由 Scanner 维护，
    # 其余（刚加进自定义列表、Scanner 还没轮到）批量补拉一次
    snaps = dict(scanner.snapshots) if scanner else {}
    missing = [c for c in codes if c not in snaps]
    if missing:
        snaps.update(await ds.snapshots(missing))

    for code, bars in data.items():
        hit, reason, ts = _eval_on_day(code, cls, bars, target, confirm_on_close)
        if not hit:
            continue
        snap = snaps.get(code) or {}
        result["matches"].append({
            "code": code,
            "name": snap.get("name") or names.get(code, code),
            "price": snap.get("price") or (bars[-1]["close"] if bars else None),
            "change_pct": snap.get("change_pct"),
            "reason": reason,
            "ts": ts,
        })
    result["matches"].sort(
        key=lambda m: (m["ts"], m.get("change_pct") or 0), reverse=True)
    result["elapsed_ms"] = int((time.time() - start) * 1000)
    return result
