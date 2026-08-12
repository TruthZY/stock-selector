# -*- coding: utf-8 -*-
"""后台历史数据下载 CLI：python download.py [options]（参数缺省取 config.DOWNLOAD_DEFAULT）

把长历史K线灌进回测缓存（kline_cache 表），跑完后验证台/回测直接命中缓存秒出。
数据源固定 BaoStock（前复权），全局串行约 2.5s/任务，慢是正常的。

只写回测缓存 kline_cache，不碰实时系统的 kline_daily/kline_min ——
前者供战法验证/回测，后者供盘中实时分析，两者用途与数据要求都不同。

示例：
  python download.py                                   # 默认：股票池 × 30m+daily，增量更新
  python download.py --mode full                       # 整段重拉覆盖
  python download.py --scope watch --period daily
  python download.py --scope codes --codes 600519,000858 --period 30m --start 2022-01-01
  python download.py --scope all --period daily        # 全市场（很慢，慎用）
  python download.py --dry-run                         # 只看任务量与预估耗时
"""
import argparse
import asyncio
import os
import sys
import time
import unicodedata
from datetime import datetime

import config
from app.backtest.downloader import (BAOSTOCK_PERIODS, DownloadConfig,
                                     Downloader, run_download)

# Windows 终端默认 GBK 编码，统一输出为 UTF-8 避免中文乱码（配合 download.bat 的 chcp 65001）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCOPE_NAMES = {"pool": "股票池", "watch": "自选", "all": "全市场", "codes": "指定代码"}
MODE_NAMES = {"incremental": "增量更新（只补缺口）", "full": "全量更新（整段重拉覆盖）"}
# 耗时经验模型：BaoStock 单连接串行，固定开销 ~1s（持锁 sleep 0.5s + 建查询）
# 加上按根数线性的传输/解析开销。实测 daily 1601根≈2.4s、30m 12808根≈12s
_TASK_BASE_SECONDS = 1.0
_BARS_PER_SECOND = 1100.0
# 自然日 -> 交易日的折算比例（约 250/365）
_TRADING_DAY_RATIO = 0.685
# 每隔多少个任务插一行 ETA
ETA_EVERY = 20
BAR = "=" * 60


def _width(s: str) -> int:
    """字符串的终端显示宽度（中文/全角算 2 列）"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    """按显示宽度右侧补空格，避免中文名把列撞歪"""
    return s + " " * max(0, width - _width(s))


def _task_seconds(period: str, start: str, end: str) -> float:
    """估算单个任务耗时：分钟周期根数是日线的 8~48 倍，耗时差一个量级，
    用统一常数会把 30m 的预估算少 5 倍，所以按周期根数估"""
    from app.backtest.loader import PERIOD_BARS_PER_DAY
    try:
        days = max((datetime.strptime(end, "%Y-%m-%d")
                    - datetime.strptime(start, "%Y-%m-%d")).days, 1)
    except ValueError:
        days = 250
    bars = days * _TRADING_DAY_RATIO * PERIOD_BARS_PER_DAY.get(period, 8)
    return _TASK_BASE_SECONDS + bars / _BARS_PER_SECOND


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60:02d}秒"
    return f"{seconds // 3600}小时{seconds % 3600 // 60:02d}分"


class ProgressPrinter:
    """把 Downloader 的进度事件打成逐行滚动日志"""

    def __init__(self):
        self.online_elapsed = []    # 联网任务耗时（跳过的不计入，用于 ETA）
        self._cooldown_shown = 0.0
        self._avg_task = 0.0        # 上游给的单任务预估（各周期均值）

    def __call__(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "start":
            self._on_start(ev)
        elif kind == "task":
            self._on_task(ev)
        elif kind == "cooldown":
            self._on_cooldown(ev)
        elif kind == "done":
            self._on_done(ev)

    def _on_start(self, ev: dict) -> None:
        total = ev["total"]
        # 按周期分别估：一轮里 30m 和 daily 的耗时差一个量级
        per_stock = sum(_task_seconds(p, ev["start"], ev["end"]) for p in ev["periods"])
        eta = per_stock * ev["stocks"]
        self._avg_task = eta / total if total else 0.0
        print(BAR)
        print("  回测数据下载（写入 kline_cache，与实时数据互不影响）")
        print(f"  方式: {MODE_NAMES.get(ev.get('mode'), ev.get('mode'))} | "
              f"数据源: BaoStock 前复权")
        print(f"  范围: {SCOPE_NAMES.get(ev['scope'], ev['scope'])} {ev['stocks']} 只 | "
              f"周期: {', '.join(ev['periods'])}")
        print(f"  区间: {ev['start']} ~ {ev['end']} | 共 {total} 个任务")
        print(f"  预估耗时: 约 {_fmt_duration(eta)}"
              f"（BaoStock 全局串行，约 {self._avg_task:.1f}s/任务）")
        print(BAR, flush=True)

    def _on_task(self, ev: dict) -> None:
        idx, total = ev["index"], ev["total"]
        head = (f"[{idx:>{len(str(total))}}/{total}] {ev['code']} "
                f"{_pad(ev['name'], 10)} {_pad(ev['period'], 6)}")
        if ev["status"] == "ok":
            body = _pad(f"+{ev['added']}根", 11) + f"{ev['elapsed']:.1f}s"
        elif ev["status"] == "cached":
            body = _pad("已覆盖跳过", 11) + f"{ev['elapsed']:.1f}s"
        elif ev["status"] == "nodata":
            # 重拉了但没新增：区间与末根重叠一天所致，说明已是最新
            label = (f"无新增(重拉{ev['refetched']}根)" if ev.get("refetched")
                     else "无新数据")
            body = _pad(label, 11) + f" {ev['elapsed']:.1f}s"
        else:
            body = f"✗ {ev['msg']}"
        print(f"{head} {body}", flush=True)

        # nodata 也发生了网络请求，同样计入 ETA 样本；只有 cached 是纯跳过
        if ev["status"] in ("ok", "nodata"):
            self.online_elapsed.append(ev["elapsed"])
        if idx % ETA_EVERY == 0 and idx < total:
            print(f"       进度 {idx}/{total} · 剩余约 {self._eta(total - idx)}", flush=True)

    def _eta(self, remaining: int) -> str:
        avg = (sum(self.online_elapsed) / len(self.online_elapsed)
               if self.online_elapsed else self._avg_task)
        return _fmt_duration(remaining * avg)

    def _on_cooldown(self, ev: dict) -> None:
        remain = ev["remain"]
        # 冷却可能持续 5 分钟，每 30s 打一行就够，不刷屏
        if self._cooldown_shown and abs(self._cooldown_shown - remain) < 30:
            return
        self._cooldown_shown = remain
        print(f"  ⏸ BaoStock 熔断冷却中，等待 {int(remain) // 60:02d}:{int(remain) % 60:02d} …",
              flush=True)

    def _on_done(self, ev: dict) -> None:
        self._cooldown_shown = 0.0
        if not ev["ok"]:
            print(f"下载失败: {ev['msg']}", flush=True)
            return   # 收尾的分隔线由 main() 统一打
        print("─" * 16 + " 汇总 " + "─" * 16)
        print(f"总计 {ev['total']}   更新 {ev['ok_count']}   已覆盖 {ev['cached']}   "
              f"无新数据 {ev.get('nodata', 0)}   失败 {ev['failed']}")
        print(f"新增 {ev['added_bars']:,} 根   耗时 {_fmt_duration(ev['elapsed'])}")


def _write_failures(result: dict) -> str:
    """失败清单落文件，附可直接复制的重试命令；无失败则删除旧文件"""
    path = os.path.join(config.BASE_DIR,
                        config.DOWNLOAD_DEFAULT.get("failed_file", "download_failed.txt"))
    failures = result.get("failures") or []
    if not failures:
        if os.path.exists(path):
            os.remove(path)
        return ""

    # 按周期归并代码，方便按周期分别重试
    by_period = {}
    for f in failures:
        by_period.setdefault(f["period"], []).append(f["code"])

    lines = [f"# 下载失败清单  生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"# 区间 {result['start']} ~ {result['end']}  共 {len(failures)} 条", "",
             f"{_pad('代码', 8)}{_pad('名称', 12)}{_pad('周期', 8)}原因"]
    for f in failures:
        lines.append(f"{_pad(f['code'], 8)}{_pad(f['name'], 12)}"
                     f"{_pad(f['period'], 8)}{f['reason']}")
    lines += ["", "# 重试命令："]
    for period, codes in by_period.items():
        lines.append(f"python download.py --scope codes --codes {','.join(codes)} "
                     f"--period {period} --start {result['start']}")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")

    print(f"失败清单已写入 {os.path.basename(path)}")
    for period, codes in by_period.items():
        show = ",".join(codes[:8]) + (f" 等{len(codes)}只" if len(codes) > 8 else "")
        print(f"重试: python download.py --scope codes --codes {show} --period {period}")
    return path


def _dry_run(cfg: DownloadConfig) -> None:
    """只算任务量与预估耗时，不发任何请求"""
    dl = Downloader(cfg)
    stocks = asyncio.run(_resolve_only(dl))
    total = len(stocks) * len(cfg.periods)
    print(BAR)
    print("  [dry-run] 不会发起任何请求")
    print(f"  方式: {MODE_NAMES.get(cfg.mode, cfg.mode)}")
    print(f"  范围: {SCOPE_NAMES.get(cfg.scope, cfg.scope)} {len(stocks)} 只 | "
          f"周期: {', '.join(cfg.periods)}")
    print(f"  区间: {cfg.start} ~ {dl.end} | 共 {total} 个任务")
    # 按周期分别算：跳过的不计耗时，30m 与 daily 单任务耗时差一个量级
    todo, eta = 0, 0.0
    for code, _ in stocks:
        for p in cfg.periods:
            if dl._missing_span(*dl.cache.stat(code, p)) is None:
                continue
            todo += 1
            eta += _task_seconds(p, cfg.start, dl.end)
    print(f"  其中已覆盖可跳过: {total - todo} 个，需联网: {todo} 个")
    print(f"  预估耗时: 约 {_fmt_duration(eta)}")
    print(BAR)


async def _resolve_only(dl: Downloader):
    try:
        return await dl._resolve_scope()
    finally:
        await dl.ds.close()


def main():
    parser = argparse.ArgumentParser(
        description="后台历史数据下载（参数缺省取 config.DOWNLOAD_DEFAULT）")
    parser.add_argument("--scope", choices=["pool", "watch", "all", "codes"],
                        help="标的范围：pool=股票池 / watch=自选 / all=全市场 / codes=指定")
    parser.add_argument("--codes", help="scope=codes 时的代码列表，逗号分隔，如 600519,000858")
    parser.add_argument("--period", help=f"K线周期，逗号分隔，可用：{','.join(BAOSTOCK_PERIODS)}")
    parser.add_argument("--start", help="下载起点 YYYY-MM-DD")
    parser.add_argument("--end", help="下载终点 YYYY-MM-DD（空=今天）")
    parser.add_argument("--mode", choices=["incremental", "full"],
                        help="incremental=增量更新，只补缺口（默认）/ full=整段重拉覆盖")
    parser.add_argument("--throttle", type=float, help="每个任务之间的间隔（秒）")
    parser.add_argument("--force", action="store_true",
                        help="先清缓存再下（比 --mode full 更彻底，用于怀疑缓存有脏数据时）")
    parser.add_argument("--no-wait", action="store_true",
                        help="数据源熔断冷却时不等待，直接跳过（默认等待）")
    parser.add_argument("--dry-run", action="store_true", help="只打印任务量与预估耗时")
    args = parser.parse_args()

    # 配置合并：config.DOWNLOAD_DEFAULT <- CLI 参数覆盖
    d = dict(config.DOWNLOAD_DEFAULT)
    for key, attr in [("scope", "scope"), ("start", "start"), ("end", "end"),
                      ("mode", "mode"), ("throttle", "throttle")]:
        v = getattr(args, attr)
        if v is not None:
            d[key] = v
    if args.codes:
        d["codes"] = [c.strip() for c in args.codes.split(",") if c.strip()]
    if args.period:
        d["periods"] = [p.strip() for p in args.period.split(",") if p.strip()]
    if args.force:
        d["force"] = True
        d["mode"] = "full"      # 清库后必然是全量
    if args.no_wait:
        d["wait_cooldown"] = False
    # --codes 时自动切 scope，省得两个参数都要写
    if args.codes and not args.scope:
        d["scope"] = "codes"
    cfg = DownloadConfig.from_dict(d)

    if args.dry_run:
        _dry_run(cfg)
        return

    try:
        result = asyncio.run(run_download(cfg, ProgressPrinter()))
    except KeyboardInterrupt:
        print("\n已中断（已下载的部分保留在缓存中，重跑会自动跳过）")
        sys.exit(130)

    if result.get("ok"):
        _write_failures(result)
    print(BAR)
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
