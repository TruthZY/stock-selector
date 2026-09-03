# -*- coding: utf-8 -*-
"""常驻调度器（唯一常驻件）：tick 轮询到点的作业槽位，触发对应短命作业。

采用"轮询 + 触发到期槽位"模型（而非精确 sleep-to-next），因为它天然实现了：
  - 错过补跑：守护进程曾在槽位时刻宕机/未起，重启后只要仍在触发窗口内，
    下一个 tick 就会补触发（超窗则放弃，不深夜补推陈旧信号）；
  - 幂等：靠 state 表，每槽只跑一次（作业A）/ 今日成功即止（作业B阶梯）；
  - 简单稳健：无需精确的下次触发时刻数学，tick 粒度 30s 对分钟级计划足够。

非交易日：周末在 tick 层直接不触发；节假日由作业内的日历哨兵拦下并记 skip。
常驻内存只有本调度器对象；作业与其重依赖都是现建现用、用完即弃。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import List, Tuple

from push import jobs
from push.settings import Settings
from push.state import State

# 触发窗口：槽位时刻之后多久内仍允许触发（含补跑）
SCAN_WINDOW_MIN = 90      # 作业A：14:00 槽位 → 14:00~15:30 内可触发/补跑
UPDATE_WINDOW_MIN = 25    # 作业B：每个阶梯时刻后 25 分钟内触发
DEFAULT_TICK_SEC = 30


class Scheduler:
    def __init__(self, settings: Settings, state: State, logger,
                 tick_sec: int = DEFAULT_TICK_SEC):
        self.s = settings
        self.state = state
        self.log = logger
        self.tick_sec = max(5, int(tick_sec))
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    # --- 计划：把配置展开成 [(HH:MM, job, period)] ---
    def slots(self) -> List[Tuple[str, str, str]]:
        out: List[Tuple[str, str, str]] = []
        ladder = [self.s.postclose_first] + list(self.s.postclose_retries)
        for period, cfg in self.s.periods.items():
            if not cfg.get("enabled"):
                continue
            for t in cfg.get("times", []):
                out.append((t, "scan", period))          # 作业A：盘中实时扫描
            for t in ladder:
                out.append((t, "update", period))        # 作业B：盘后数据更新
            # 作业C：盘后扫描推送（只用收盘缓存），须排在作业B末次之后
            if period == "daily" and self.s.postclose_scan_enabled:
                out.append((self.s.postclose_scan_time, "scan_close", period))
        return sorted(out, key=lambda x: x[0])

    @staticmethod
    def _slot_dt(hhmm: str, day0: datetime) -> datetime:
        hh, mm = hhmm.split(":")
        return day0.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)

    # --- 单个 tick：触发所有"已到点、在窗口内、未跑过"的槽位 ---
    async def _tick(self) -> None:
        now = datetime.now()
        if now.weekday() >= 5:            # 周末不触发
            return
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        date_str = now.strftime("%Y-%m-%d")
        for hhmm, job, period in self.slots():
            slot_dt = self._slot_dt(hhmm, day0)
            if now < slot_dt:
                continue                  # 未到点
            window = SCAN_WINDOW_MIN if job in ("scan", "scan_close") else UPDATE_WINDOW_MIN
            if now > slot_dt + timedelta(minutes=window):
                continue                  # 超出触发窗口，等明天
            await self._maybe_run(job, period, hhmm, date_str)

    async def _maybe_run(self, job: str, period: str, slot: str, date_str: str) -> None:
        # 幂等门禁
        if job in ("scan", "scan_close"):
            # 每槽只跑一次（ok/fail/skip 都算跑过），避免失败后每 tick 重跑刷屏
            if self.state.is_done(job, date_str, period, slot=slot, status=None):
                return
        else:
            # 今日已成功 → 跳过后续阶梯；本槽已尝试过 → 等下一个阶梯时刻
            if self.state.is_done("update", date_str, period, status="ok"):
                return
            if self.state.is_done("update", date_str, period, slot=slot, status=None):
                return

        self.log.info("触发 %s[%s] @%s", job, period, slot)
        try:
            if job == "scan":
                await self._run_scan(period, slot, date_str)
            elif job == "scan_close":
                await self._run_scan_close(period, slot, date_str)
            else:
                await self._run_update(period, slot, date_str)
        except Exception as e:
            self.log.exception("作业 %s[%s]@%s 异常", job, period, slot)
            self.state.mark(job, date_str, period, slot, "fail",
                            detail=f"{type(e).__name__}: {e}")
            await self._alert(f"作业 {job}[{period}] @{slot} 异常：{type(e).__name__}: {e}")

    async def _run_scan(self, period: str, slot: str, date_str: str) -> None:
        res = await jobs.job_scan_push(
            self.s, period=period, push=True, scan_time=slot,
            logger=self.log, check_calendar=True)
        if res.was_skipped:
            self.state.mark("scan", date_str, period, slot, "skip",
                            detail=res.skip_reason)
        elif res.ok and not res.degraded:
            self.state.mark("scan", date_str, period, slot, "ok",
                            matches=len(res.matches), coverage=res.coverage,
                            elapsed_ms=res.elapsed_ms, detail=res.msg)
        else:
            self.state.mark("scan", date_str, period, slot, "fail",
                            coverage=res.coverage, elapsed_ms=res.elapsed_ms,
                            detail=res.msg)

    async def _run_scan_close(self, period: str, slot: str, date_str: str) -> None:
        res = await jobs.job_scan_postclose(
            self.s, period=period, push=True, scan_time=slot, logger=self.log)
        if res.was_skipped:
            self.state.mark("scan_close", date_str, period, slot, "skip",
                            detail=res.skip_reason)
        elif res.ok and not res.degraded:
            self.state.mark("scan_close", date_str, period, slot, "ok",
                            matches=len(res.matches), coverage=res.coverage,
                            elapsed_ms=res.elapsed_ms, detail=res.msg)
        else:
            self.state.mark("scan_close", date_str, period, slot, "fail",
                            coverage=res.coverage, elapsed_ms=res.elapsed_ms,
                            detail=res.msg)

    async def _run_update(self, period: str, slot: str, date_str: str) -> None:
        res = await jobs.job_postclose_update(
            self.s, period=period, logger=self.log, check_calendar=True)
        self.state.mark("update", date_str, period, slot, res.get("status", "fail"),
                        coverage=res.get("ratio", 0.0),
                        elapsed_ms=res.get("elapsed_ms", 0),
                        detail=f"landed={res.get('landed')}/{res.get('total')}")

    async def _alert(self, text: str) -> None:
        """失败告警（尽力而为，不阻断调度）。"""
        try:
            if self.s.channel == "dingtalk" and self.s.has_dingtalk:
                from push.pushers import get_pusher
                p = get_pusher(self.s)
                try:
                    await p.send_text(f"【推送系统告警】{text}")
                finally:
                    await p.aclose()
        except Exception:
            self.log.warning("告警发送失败（忽略）")

    # --- 主循环 ---
    async def run_forever(self) -> None:
        self.log.info("调度器启动 tick=%ss | 槽位=%s", self.tick_sec, self.slots())
        while not self._stop:
            try:
                await self._tick()
            except Exception as e:
                self.log.exception("tick 异常：%s", e)
            # 可中断的秒级 sleep：便于 stop() 及时生效、也抗系统时钟跳变
            for _ in range(self.tick_sec):
                if self._stop:
                    break
                await asyncio.sleep(1)
        self.log.info("调度器已停止")
