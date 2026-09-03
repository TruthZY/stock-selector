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


class PushScanner:
    def __init__(self, pool: List[tuple], ds, cache, rule_key: str,
                 period: str = "daily", params: Optional[Dict] = None,
                 mode: str = "live", budget_sec: int = 120,
                 concurrency: int = 10, min_coverage: float = 0.6,
                 min_bars: int = detector.DEFAULT_MIN_BARS,
                 min_mid_angle: Optional[float] = None,
                 rank_angle_w: float = 1.0, rank_vol_w: float = 10.0):
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

        # 1. 快照（分批，带 deadline）
        snaps = await datafeed.fetch_snapshots(self.ds, codes, deadline=deadline)
        # 2. 历史（并发，带 deadline；优先缓存）
        hists = await datafeed.load_histories(
            self.ds, self.cache, codes, self.period,
            deadline=deadline, concurrency=self.concurrency)

        # 3. 逐股装配 + 检测
        matches: List[dict] = []
        with_data = 0
        skipped = 0
        for code in codes:
            hist = hists.get(code)
            snap = snaps.get(code)
            bars = datafeed.build_bars(hist, snap, self.period)
            # 数据齐全度：daily 需历史 + 今日快照合成成功（bars 末根是今日）
            if len(bars) < self.min_bars:
                skipped += 1
                continue
            if self.period == "daily" and (not bars or bars[-1]["ts"][:10] != today):
                # 没拿到今日快照 → 当日bar缺失，跳过（不计入 with_data）
                skipped += 1
                continue
            with_data += 1
            hit = detector.detect_one(code, names.get(code, code), bars, rule_cls,
                                      params=self.params, mode=self.mode,
                                      min_bars=self.min_bars,
                                      min_mid_angle=self.min_mid_angle)
            if hit is not None:
                # 附加快照里的实时涨跌幅，便于排序/展示
                if snap:
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
            skipped=skipped, elapsed_ms=elapsed,
            msg=f"命中 {len(matches)} / 参与判定 {with_data} / 计划 {len(codes)}"
                f"（覆盖率 {coverage*100:.0f}%，跳过 {skipped}，耗时 {elapsed}ms）")
