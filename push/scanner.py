# -*- coding: utf-8 -*-
"""PushScanner：推送系统自建的**短命有界**扫描器（非 app/scanner.py 的常驻 Scanner）。

生命周期：到点实例化 → 有界扫描（扫完或超时即止）→ 产出命中清单 → 由调用方销毁。
不常驻任何内存态（快照/历史/规则实例都是本次运行内临时对象，用完即弃）。

流程（作业A）：
  1. 分批拉实时快照（腾讯，50/批）；
  2. 并发加载日线历史（优先缓存，陈旧才补，deadline 到点停止发起新请求）；
  3. 逐股 build_bars（历史 + 快照合成当日日K）→ detector 跑买入战法；
  4. 汇总命中 + 覆盖率；覆盖率过低标记 degraded（供上层改推降级告警）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from push import datafeed, detector


@dataclass
class ScanResult:
    ok: bool
    period: str
    rule: str
    date: str
    matches: List[dict] = field(default_factory=list)
    scanned: int = 0            # 计划扫描的股票数
    with_data: int = 0          # 数据齐全（历史+快照）并参与判定的股票数
    coverage: float = 0.0       # with_data / scanned
    degraded: bool = False      # 覆盖率低于阈值 → 数据异常，别误报"无信号"
    skipped: int = 0            # 数据不齐/历史不足被跳过
    elapsed_ms: int = 0
    msg: str = ""
    # 非交易日跳过标记（作业被日历哨兵拦下，未真正扫描）
    was_skipped: bool = False
    skip_reason: str = ""
    # 推送结果（None=未推送/干跑，True/False=推送成功与否）
    push_ok: Optional[bool] = None
    # 本次扫描采集到的报错明细（个股取数/检测异常、快照批失败、超时截止等），
    # 供上层把"⚠️ 异常/错误"段附在推送末尾
    errors: List[str] = field(default_factory=list)


class PushScanner:
    def __init__(self, pool: List[tuple], ds, cache, rule_key: str,
                 period: str = "daily", params: Optional[Dict] = None,
                 mode: str = "live", budget_sec: int = 120,
                 concurrency: int = 10, min_coverage: float = 0.6,
                 min_bars: int = detector.DEFAULT_MIN_BARS,
                 min_mid_angle: Optional[float] = None,
                 rank_angle_w: float = 1.0, rank_vol_w: float = 10.0,
                 source: str = "live"):
        self.pool = list(pool)
        self.ds = ds
        self.cache = cache
        self.rule_key = rule_key
        self.period = period
        self.params = params or {}
        self.mode = mode
        self.budget_sec = budget_sec
        self.concurrency = concurrency
        self.min_coverage = min_coverage
        self.min_bars = min_bars
        self.min_mid_angle = min_mid_angle
        self.rank_angle_w = rank_angle_w
        self.rank_vol_w = rank_vol_w
        # source: "live"=拉实时快照合成当日bar(盘中) / "cache"=只用缓存收盘bar(盘后)
        self.source = source

    async def scan(self) -> ScanResult:
        t0 = time.time()
        deadline = t0 + self.budget_sec
        today = datafeed.today_str()

        rule_cls = detector.get_rule_class(self.rule_key)
        if rule_cls is None:
            return ScanResult(False, self.period, self.rule_key, today,
                              msg=f"未知买入规则 {self.rule_key!r}，可用："
                                  f"{', '.join(detector.list_rule_keys()) or '无'}")

        codes = [c for c, _ in self.pool]
        names = dict(self.pool)
        if not codes:
            return ScanResult(False, self.period, self.rule_key, today, msg="股票池为空")

        errors: List[str] = []   # 本次扫描采集的报错明细
        # 1&2. 取数：盘中=实时快照+历史；盘后=只读缓存收盘bar（不联网）
        snaps: Dict[str, dict] = {}
        if self.source == "cache":
            hists = {}
            for code in codes:
                try:
                    h = self.cache.get_all(code, self.period)
                except Exception as e:
                    h = None
                    errors.append(f"{code} 缓存读取失败: {type(e).__name__}: {e}")
                if h:
                    hists[code] = h
        else:
            snaps = await datafeed.fetch_snapshots(self.ds, codes, deadline=deadline,
                                                   errors=errors)
            hists = await datafeed.load_histories(
                self.ds, self.cache, codes, self.period,
                deadline=deadline, concurrency=self.concurrency, errors=errors)

        # 3. 逐股装配 + 检测
        matches: List[dict] = []
        with_data = 0
        skipped = 0
        for code in codes:
            hist = hists.get(code)
            snap = snaps.get(code)
            if self.source == "cache":
                bars = datafeed.build_bars_close(hist, self.period, today)
            else:
                bars = datafeed.build_bars(hist, snap, self.period)
            # 数据齐全度：daily 需今日那根在位（盘中来自快照合成，盘后来自缓存收盘bar）
            if len(bars) < self.min_bars:
                skipped += 1
                continue
            if self.period == "daily" and (not bars or bars[-1]["ts"][:10] != today):
                # 当日bar缺失（盘中没拿到快照 / 盘后作业B尚未落库）→ 跳过
                skipped += 1
                continue
            with_data += 1
            hit = detector.detect_one(code, names.get(code, code), bars, rule_cls,
                                      params=self.params, mode=self.mode,
                                      min_bars=self.min_bars,
                                      min_mid_angle=self.min_mid_angle,
                                      errors=errors)
            if hit is not None:
                if self.source == "cache":
                    # 盘后：价格/涨跌幅取自缓存收盘bar（今收 vs 昨收）
                    hit["price"] = bars[-1].get("close")
                    if len(bars) >= 2 and bars[-2].get("close"):
                        hit["change_pct"] = (bars[-1]["close"] / bars[-2]["close"] - 1) * 100
                elif snap:
                    # 盘中：附加快照里的实时涨跌幅，便于排序/展示
                    hit["change_pct"] = snap.get("change_pct")
                    hit["price"] = snap.get("price") or hit.get("price")
                matches.append(hit)

        coverage = (with_data / len(codes)) if codes else 0.0
        # 质量分：中轨角度(趋势) + 量比(资金)，均越高越好；角度缺失按极低处理
        for m in matches:
            a = m.get("mid_angle")
            a = a if a is not None else -999.0
            m["quality"] = (self.rank_angle_w * a
                            + self.rank_vol_w * (m.get("vol_ratio", 0.0) - 1.0))
        # 排序：命中条件数 desc → 质量分 desc；同分按代码 asc（两趟稳定排序）
        matches.sort(key=lambda m: str(m.get("code", "")))
        matches.sort(key=lambda m: (m.get("n_conditions", 0), m.get("quality", 0.0)),
                     reverse=True)
        elapsed = int((time.time() - t0) * 1000)
        return ScanResult(
            ok=True, period=self.period, rule=self.rule_key, date=today,
            matches=matches, scanned=len(codes), with_data=with_data,
            coverage=round(coverage, 4), degraded=(coverage < self.min_coverage),
            skipped=skipped, elapsed_ms=elapsed, errors=errors,
            msg=f"命中 {len(matches)} / 参与判定 {with_data} / 计划 {len(codes)}"
                f"（覆盖率 {coverage*100:.0f}%，跳过 {skipped}，耗时 {elapsed}ms）")
