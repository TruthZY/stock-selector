# -*- coding: utf-8 -*-
"""作业编排：把 datafeed/detector/scanner/formatter/pushers/calendar 串成两个短命作业。

作业A job_scan_push        ：盘中扫描 → 合并推送（含交易日历哨兵，非交易日跳过）。
作业B job_postclose_update ：盘后把当日已收盘K线增量补进 kline_cache + 落库校验。

所有重依赖（Store/DataSource/KlineCache/PushScanner）都在作业内现建、用完释放，
不常驻内存；返回值带明确状态（ok/skip/fail）供调度器写 state 做幂等与重试。
"""
from __future__ import annotations

import time
from typing import Optional

from push import datafeed, formatter
from push.scanner import PushScanner, ScanResult
from push.settings import Settings


def _resolve_pool(store, fallback):
    try:
        pool = store.get_stocks()
    except Exception:
        pool = []
    return pool or list(fallback)


async def job_scan_push(settings: Settings, period: str = "daily",
                        dry_run: bool = False, push: bool = True,
                        top_n: int = 0, scan_time: str = "14:00",
                        logger=None, check_calendar: bool = True) -> ScanResult:
    """作业A：对指定周期跑一次盘中扫描并（可选）推送。返回 ScanResult。

    check_calendar=True 时先做交易日哨兵：非交易日（周末/节假日）直接跳过、不推送，
    避免用陈旧快照合成"假的当日日K"产生错误信号。
    """
    log = logger
    pcfg = settings.periods.get(period) or {}
    rule_key = pcfg.get("rule", "accumulation_detect")
    mode = pcfg.get("mode", "live")
    rule_params = pcfg.get("params") or {}
    eff_top_n = top_n or int(pcfg.get("top_n", 0) or 0)
    min_mid_angle = pcfg.get("min_mid_angle", None)

    from app.store import Store
    from app.datasource import DataSource
    from app.backtest.cache import KlineCache
    import config as app_config
    from push import calendar as pcal

    store = Store()
    ds = DataSource()
    cache = KlineCache()
    pool = _resolve_pool(store, app_config.BUILTIN_POOL)
    codes = [c for c, _ in pool]
    today = datafeed.today_str()
    scanner = None
    t0 = time.time()
    try:
        if check_calendar:
            status, reason = await pcal.classify_trading_day(ds, codes, today)
            if status == "closed":
                if log:
                    log.info("作业A[%s] 跳过：%s", period, reason)
                return ScanResult(ok=True, period=period, rule=rule_key, date=today,
                                  scanned=len(codes), was_skipped=True,
                                  skip_reason=reason, msg=f"非交易日跳过：{reason}")
        scanner = PushScanner(
            pool, ds, cache, rule_key=rule_key, period=period, mode=mode,
            params=rule_params, budget_sec=settings.scan_budget_sec,
            concurrency=settings.gather_concurrency, min_coverage=settings.min_coverage,
            min_mid_angle=min_mid_angle,
            rank_angle_w=settings.rank_angle_w, rank_vol_w=settings.rank_vol_w,
        )
        result = await scanner.scan()
    finally:
        try:
            await ds.close()
        except Exception:
            pass
        scanner = None
        store = None
        cache = None

    if log:
        log.info("作业A[%s] 完成：%s", period, result.msg)

    # 组装消息（覆盖率过低走降级告警）
    if result.ok and result.degraded:
        title, md = formatter.build_degraded_message(result, scan_time)
    elif result.ok:
        title, md = formatter.build_message(result, scan_time, top_n=eff_top_n)
    else:
        title, md = "推送系统错误", f"### ❌ 扫描失败\n\n{result.msg}"

    if dry_run:
        print("=== DRY-RUN：作业A 结果（不推送）===")
        print(result.msg)
        print(f"[标题] {title}")
        print("[正文]")
        print(md)
        return result

    if push:
        from push.pushers import get_pusher
        if settings.channel == "dingtalk" and not settings.has_dingtalk:
            if log:
                log.warning("未配置钉钉凭据，跳过推送（仅完成扫描）")
            print("（未配置钉钉凭据，已跳过推送；扫描结果如上）")
            return result
        pusher = get_pusher(settings)
        try:
            pr = await pusher.send_markdown(title, md)
        finally:
            await pusher.aclose()
        if log:
            log.info("推送结果：ok=%s detail=%s", pr.ok, pr.detail)
        print(f"推送结果：{'✓ 成功' if pr.ok else '✗ 失败'}（{pr.detail}）")
        result.push_ok = pr.ok  # type: ignore[attr-defined]

    result.elapsed_ms = int((time.time() - t0) * 1000)
    return result


async def job_postclose_update(settings: Settings, period: str = "daily",
                               dry_run: bool = False, logger=None,
                               check_calendar: bool = True,
                               min_landed_ratio: float = 0.6) -> dict:
    """作业B：盘后把当日已收盘K线增量补进 kline_cache，并做落库校验。

    返回 dict：{status: ok|skip|fail, landed, total, ratio, fetched, elapsed_ms, ...}
      ok   ：落库比例 >= min_landed_ratio（停牌股无当日bar，故阈值<1）
      skip ：非交易日
      fail ：落库不足（数据源尚未发布/异常）→ 调度器在后续阶梯时刻重试
    """
    log = logger
    from app.store import Store
    from app.datasource import DataSource
    from app.backtest.cache import KlineCache
    import config as app_config
    from push import calendar as pcal

    store = Store()
    ds = DataSource()
    cache = KlineCache()
    pool = _resolve_pool(store, app_config.BUILTIN_POOL)
    codes = [c for c, _ in pool]
    today = datafeed.today_str()
    t0 = time.time()
    try:
        if check_calendar:
            status, reason = await pcal.classify_trading_day(ds, codes, today)
            if status == "closed":
                if log:
                    log.info("作业B[%s] 跳过：%s", period, reason)
                return {"status": "skip", "ok": False, "date": today,
                        "period": period, "reason": reason}
        # 盘后不赶时间，给足预算（至少 120s）
        deadline = time.time() + max(settings.scan_budget_sec * 2, 120)
        fetched, landed = await datafeed.refresh_to_today(
            ds, cache, codes, period, today,
            deadline=deadline, concurrency=settings.gather_concurrency)
    finally:
        try:
            await ds.close()
        except Exception:
            pass
        store = None
        cache = None

    total = len(codes)
    ratio = (len(landed) / total) if total else 0.0
    ok = ratio >= min_landed_ratio
    res = {"status": "ok" if ok else "fail", "ok": ok, "date": today,
           "period": period, "fetched": len(fetched), "landed": len(landed),
           "total": total, "ratio": round(ratio, 4),
           "elapsed_ms": int((time.time() - t0) * 1000)}
    if log:
        log.info("作业B[%s] %s", period, res)
    if dry_run:
        print("=== 作业B 结果 ===")
        print(res)
    return res
