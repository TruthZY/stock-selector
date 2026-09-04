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


def _record_and_attach(state, session: str, result, period: str,
                       window_days: int, keep_dates: int) -> None:
    """把本次扫描命中写入滑动记录(session)，再把近 window_days 个扫描日的
    盘前/盘后命中次数注入每只 match（cnt_pre/cnt_post），供 formatter 展示。

    顺序：先记录本次(含今日) → 再统计，故今日命中计入自身 session 的次数。
    记录的是完整命中清单(result.matches)，不是只记 top_n。
    """
    try:
        state.record_hits(session, result.date, period, result.matches)
        codes = [str(m.get("code", "")) for m in result.matches]
        pre = state.hit_counts("pre", period, codes, window_days)
        post = state.hit_counts("post", period, codes, window_days)
        for m in result.matches:
            c = str(m.get("code", ""))
            m["cnt_pre"] = pre.get(c, 0)
            m["cnt_post"] = post.get(c, 0)
        state.prune_hits(keep_dates=keep_dates)
    except Exception:
        # 滑动记录失败不应阻断推送：次数缺失时 formatter 会自动省略标注
        for m in result.matches:
            m.setdefault("cnt_pre", None)
            m.setdefault("cnt_post", None)


async def job_scan_push(settings: Settings, period: str = "daily",
                        dry_run: bool = False, push: bool = True,
                        top_n: int = 0, scan_time: str = "14:00",
                        logger=None, check_calendar: bool = True,
                        session: str = "pre", record: bool = True) -> ScanResult:
    """作业A：对指定周期跑一次盘中扫描并（可选）推送。返回 ScanResult。

    check_calendar=True 时先做交易日哨兵：非交易日（周末/节假日）直接跳过、不推送，
    避免用陈旧快照合成"假的当日日K"产生错误信号。
    session="pre"：命中写入盘前滑动记录；record=False 时只扫描不留痕（临时测试用）。
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
    from push.state import State

    store = Store()
    ds = DataSource()
    cache = KlineCache()
    state = State(settings.state_dir)
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
    except Exception as e:
        # 扫描整体异常：记日志（traceback 落 push.log，稍后由日志尾附进推送），
        # 造一个 ok=False 的结果，让下方组装"❌ 扫描失败 + 错误摘要 + 日志尾"并推送
        if log:
            log.exception("作业A[%s] 扫描异常", period)
        result = ScanResult(ok=False, period=period, rule=rule_key, date=today,
                            scanned=len(codes),
                            msg=f"扫描异常：{type(e).__name__}: {e}",
                            errors=[f"作业A[{period}] {type(e).__name__}: {e}"])
    finally:
        try:
            await ds.close()
        except Exception:
            pass
        scanner = None
        store = None
        cache = None
        # 注意：state 不在此清空——_record_and_attach 在下方还要用它写滑动记录。
        # State 是轻量对象（每次调用即开即关连接），保留引用无内存负担。

    if log and result.ok:
        log.info("作业A[%s] 完成：%s", period, result.msg)

    # 报错/降级/失败时附最近日志尾（已脱敏），让钉钉里就能初步定位
    from push.logtail import tail_log
    need_tail = bool(getattr(result, "errors", None)) or result.degraded or not result.ok
    log_tail = tail_log(settings.log_dir, 20) if need_tail else None

    # 组装消息（覆盖率过低走降级告警；正常则记录命中并注入近N日次数）
    if result.ok and result.degraded:
        title, md = formatter.build_degraded_message(result, scan_time, log_tail=log_tail)
    elif result.ok:
        if record:
            _record_and_attach(state, session, result, period,
                               settings.hit_window_days, settings.hit_keep_dates)
        title, md = formatter.build_message(
            result, scan_time, top_n=eff_top_n,
            window_days=settings.hit_window_days, log_tail=log_tail)
    else:
        title = f"推送系统错误 · 作业A[{period}] 扫描失败"
        lines = [f"### ❌ 扫描失败 · {period} · {scan_time}", "",
                 f"> {result.msg}", "", f"· {today}"]
        lines += formatter.build_error_section(result.errors, log_tail)
        md = "\n".join(lines)

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


async def job_scan_postclose(settings: Settings, period: str = "daily",
                             dry_run: bool = False, push: bool = True,
                             top_n: int = 0, scan_time: str = "21:00",
                             logger=None, record: bool = True) -> ScanResult:
    """作业C：盘后扫描推送（21:00）——**只用收盘缓存数据，不联网、不碰实时快照**。

    依赖作业B已把当日收盘bar落库：以缓存中"今日bar在位的股票占比"(coverage)作为
    数据就绪门禁——低于阈值即判为非交易日/数据未就绪，静默跳过（不推送、不告警，
    避免周末节假日刷屏）。命中写入 post 滑动记录，推送精简榜单(排名+名称+代码+近N日次数)。
    """
    log = logger
    pcfg = settings.periods.get(period) or {}
    rule_key = pcfg.get("rule", "accumulation_detect")
    rule_params = pcfg.get("params") or {}
    eff_top_n = (top_n or settings.postclose_scan_top_n
                 or int(pcfg.get("top_n", 0) or 0))
    min_mid_angle = pcfg.get("min_mid_angle", None)

    from app.store import Store
    from app.backtest.cache import KlineCache
    import config as app_config
    from push.state import State

    store = Store()
    cache = KlineCache()
    state = State(settings.state_dir)
    pool = _resolve_pool(store, app_config.BUILTIN_POOL)
    today = datafeed.today_str()
    t0 = time.time()
    scanner = None
    try:
        # source="cache" + mode="close"：只读缓存收盘bar，ds 传 None（该路径不联网）
        scanner = PushScanner(
            pool, None, cache, rule_key=rule_key, period=period, mode="close",
            params=rule_params, budget_sec=settings.scan_budget_sec,
            concurrency=settings.gather_concurrency, min_coverage=settings.min_coverage,
            min_mid_angle=min_mid_angle,
            rank_angle_w=settings.rank_angle_w, rank_vol_w=settings.rank_vol_w,
            source="cache")
        result = await scanner.scan()
    except Exception as e:
        if log:
            log.exception("作业C[%s] 盘后扫描异常", period)
        result = ScanResult(ok=False, period=period, rule=rule_key, date=today,
                            scanned=len(pool),
                            msg=f"盘后扫描异常：{type(e).__name__}: {e}",
                            errors=[f"作业C[{period}] {type(e).__name__}: {e}"])
    finally:
        scanner = None
        store = None
        cache = None

    # 数据就绪门禁：盘后靠作业B落库的今日收盘bar；覆盖率过低=非交易日/未就绪→静默跳过
    if result.ok and result.coverage < settings.min_coverage:
        reason = (f"盘后收盘数据未就绪/非交易日（今日bar覆盖率 "
                  f"{result.coverage*100:.0f}% < {settings.min_coverage*100:.0f}%）")
        if log:
            log.info("作业C[%s] 跳过：%s", period, reason)
        return ScanResult(ok=True, period=period, rule=rule_key, date=today,
                          scanned=len(pool), with_data=result.with_data,
                          coverage=result.coverage, was_skipped=True,
                          skip_reason=reason, msg=reason)

    if log and result.ok:
        log.info("作业C[%s] 完成：%s", period, result.msg)

    # 报错/失败时附最近日志尾（已脱敏）
    from push.logtail import tail_log
    need_tail = bool(getattr(result, "errors", None)) or not result.ok
    log_tail = tail_log(settings.log_dir, 20) if need_tail else None

    if result.ok:
        if record:
            _record_and_attach(state, "post", result, period,
                               settings.hit_window_days, settings.hit_keep_dates)
        title, md = formatter.build_postclose_message(
            result, scan_time, top_n=eff_top_n,
            window_days=settings.hit_window_days, log_tail=log_tail)
    else:
        title = f"推送系统错误 · 作业C[{period}] 盘后扫描失败"
        lines = [f"### ❌ 盘后扫描失败 · {period} · {scan_time}", "",
                 f"> {result.msg}", "", f"· {today}"]
        lines += formatter.build_error_section(result.errors, log_tail)
        md = "\n".join(lines)

    if dry_run:
        print("=== DRY-RUN：作业C(盘后) 结果（不推送）===")
        print(result.msg)
        print(f"[标题] {title}")
        print("[正文]")
        print(md)
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    if push:
        from push.pushers import get_pusher
        if settings.channel == "dingtalk" and not settings.has_dingtalk:
            if log:
                log.warning("未配置钉钉凭据，跳过推送（仅完成盘后扫描）")
            print("（未配置钉钉凭据，已跳过推送；扫描结果如上）")
            result.elapsed_ms = int((time.time() - t0) * 1000)
            return result
        pusher = get_pusher(settings)
        try:
            pr = await pusher.send_markdown(title, md)
        finally:
            await pusher.aclose()
        if log:
            log.info("盘后推送结果：ok=%s detail=%s", pr.ok, pr.detail)
        print(f"盘后推送结果：{'✓ 成功' if pr.ok else '✗ 失败'}（{pr.detail}）")
        result.push_ok = pr.ok  # type: ignore[attr-defined]

    result.elapsed_ms = int((time.time() - t0) * 1000)
    return result
