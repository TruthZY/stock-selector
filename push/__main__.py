# -*- coding: utf-8 -*-
"""推送系统命令行入口

P1 已实现：
  --test-push     发一条测试消息到配置的渠道（钉钉加签），验证密钥/网络/格式
  --dry-run       只组装并打印消息与脱敏后的目标信息，不真正发送（无需真实密钥）

P2/P3 占位（尚未实现，运行会给出提示）：
  --once          手动跑一次盘中扫描推送（作业A）
  --update        手动跑一次盘后数据更新（作业B）
  --daemon        常驻调度器

示例：
  python -m push --test-push --dry-run
  DINGTALK_WEBHOOK=... DINGTALK_SECRET=SEC... python -m push --test-push
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from push.logsetup import setup_logging
from push.settings import load_settings

# Windows GBK 控制台下，emoji/中文直出会 UnicodeEncodeError 或乱码；统一切 UTF-8
# （与项目其它入口 backtest.py 一致的做法）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 一条贴近真实日K合并推送的样例，便于肉眼验收钉钉里的排版
_SAMPLE_MARKDOWN = """### 📈 建仓信号 · 日K · 盘中预估 14:00（未收盘）

> 命中 **3** 只 / 扫描 124 只 · 覆盖率 100% · 2026-09-03

**1. 老板电器（002508）** 现价 18.62 `+2.31%`
　建仓信号：BOLL下轨上移，低点抬高(17.90→18.20)，低位23%，量比1.4

**2. 五粮液（000858）** 现价 128.50 `+1.05%`
　建仓信号：BOLL下轨上移，低位31%，量比1.2

**3. 招商银行（600036）** 现价 38.20 `+0.42%`
　建仓信号：低点抬高(37.10→37.80)，低位28%，量比1.1

---
_盘中预估，尾盘可能变化，仅供参考，不构成投资建议_"""


def _mask(url: str) -> str:
    """把 webhook URL 打码到只剩 host，避免测试输出里泄露 access_token。"""
    if not url:
        return "<未配置>"
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}?access_token=***"
    except Exception:
        return "<redacted>"


def _print_config_status(s) -> None:
    print("=== 推送系统配置状态 ===")
    print(f"  渠道 channel      : {s.channel}")
    print(f"  钉钉 webhook      : {_mask(s.dingtalk_webhook)}")
    print(f"  钉钉 secret       : {'已配置(' + s.dingtalk_secret[:3] + '***)' if s.dingtalk_secret else '<未配置>'}")
    print(f"  钉钉关键词        : {s.dingtalk_keyword or '<无>'}")
    print(f"  启用周期          : {', '.join(s.enabled_periods()) or '<无>'}")
    print(f"  日K触发时刻       : {s.periods['daily']['times']}")
    print(f"  日K买入战法       : {s.periods['daily']['rule']}")
    print(f"  盘中扫描超时预算  : {s.scan_budget_sec}s")
    print(f"  覆盖率兜底阈值    : {s.min_coverage}")
    print(f"  盘后首跑/重试     : {s.postclose_first} / {s.postclose_retries}")
    print(f"  重试次数/退避基数 : {s.push_max_retries} / {s.push_retry_backoff}s")
    print(f"  日志级别/目录     : {s.log_level} / {s.log_dir}")


async def _do_test_push(s, dry_run: bool, text: str) -> int:
    title = "推送系统测试"
    markdown = text or _SAMPLE_MARKDOWN
    if dry_run:
        print("=== DRY-RUN：以下为将要发送的消息（不联网、不需真实密钥）===")
        print(f"[标题] {title}")
        print("[正文 markdown]")
        print(markdown)
        print(f"[目标] channel={s.channel} url={_mask(s.dingtalk_webhook)}")
        print("=== DRY-RUN 结束 ===")
        return 0

    if s.channel == "dingtalk" and not s.has_dingtalk:
        print("✗ 钉钉凭据不足，无法真实发送。请设置环境变量：", file=sys.stderr)
        print("    DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx", file=sys.stderr)
        print("  并二选一（对应机器人的安全设置）：", file=sys.stderr)
        print("    加签模式  : DINGTALK_SECRET=SECxxxxxxxx", file=sys.stderr)
        print("    关键词模式: DINGTALK_KEYWORD=你的关键词", file=sys.stderr)
        print("  或先跑 `python -m push --test-push --dry-run` 查看排版。", file=sys.stderr)
        return 2

    from push.pushers import get_pusher
    pusher = get_pusher(s)
    try:
        # 附带时间戳，便于在钉钉里确认到达
        body = f"{markdown}\n\n_测试发送于 {time.strftime('%Y-%m-%d %H:%M:%S')}_"
        result = await pusher.send_markdown(title, body)
    finally:
        await pusher.aclose()

    if result.ok:
        print(f"✓ 测试消息已发送（channel={result.channel}, {result.detail}）")
        return 0
    print(f"✗ 测试消息发送失败：{result.detail}", file=sys.stderr)
    return 1


def _not_implemented(what: str, phase: str) -> int:
    print(f"『{what}』属于 {phase} 阶段，尚未实现。", file=sys.stderr)
    print(f"  P1 当前可用：--test-push（配 --dry-run 可离线预览）。", file=sys.stderr)
    return 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m push", description="股票推送系统（独立模块）")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--test-push", action="store_true", help="发一条测试消息验证渠道")
    g.add_argument("--once", action="store_true", help="手动跑一次盘中扫描推送（作业A）")
    g.add_argument("--update", action="store_true", help="手动跑一次盘后数据更新（作业B）")
    g.add_argument("--daemon", action="store_true", help="常驻调度器（自动按点触发作业A/B）")
    g.add_argument("--show-config", action="store_true", help="打印当前配置状态后退出")
    parser.add_argument("--period", default="daily", help="周期（daily/30m），默认 daily")
    parser.add_argument("--dry-run", action="store_true", help="只组装打印、不真正发送")
    parser.add_argument("--no-push", action="store_true", help="只扫描出命中清单，不推送")
    parser.add_argument("--top-n", type=int, default=0, help="合并消息只取前 N 只（0=全部）")
    parser.add_argument("--tick", type=int, default=30, help="守护轮询间隔秒（--daemon）")
    parser.add_argument("--no-calendar", action="store_true", help="跳过交易日哨兵（手动测试用）")
    parser.add_argument("--text", default="", help="自定义测试消息正文（默认用样例榜单）")
    parser.add_argument("--log-level", default="", help="覆盖日志级别")
    args = parser.parse_args(argv)

    s = load_settings()
    if args.log_level:
        s.log_level = args.log_level.upper()
    # 日志脱敏：把真实密钥登记进过滤器
    setup_logging(level=s.log_level, log_dir=s.log_dir,
                  secrets=[s.dingtalk_webhook, s.dingtalk_secret, s.webhook_token])

    if args.show_config:
        _print_config_status(s)
        return 0
    if args.test_push:
        if not args.dry_run:
            _print_config_status(s)
            print()
        return asyncio.run(_do_test_push(s, args.dry_run, args.text))
    if args.once:
        import logging
        from push.jobs import job_scan_push
        log = logging.getLogger("push")
        result = asyncio.run(job_scan_push(
            s, period=args.period, dry_run=args.dry_run,
            push=not args.no_push, top_n=args.top_n, logger=log,
            check_calendar=not args.no_calendar))
        print()
        print(f"=== 作业A[{args.period}] 命中清单 ===")
        print(result.msg or ("扫描失败：" + result.msg))
        if result.matches:
            for i, m in enumerate(result.matches, 1):
                chg = m.get("change_pct")
                chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
                print(f"{i:2}. {m.get('name','')}({m.get('code','')}) "
                      f"现价{m.get('price') if m.get('price') is not None else '—'} "
                      f"{chg_s}  {m.get('reason','')}")
        else:
            print("（无命中）")
        return 0 if result.ok else 1
    if args.update:
        import logging
        from push.jobs import job_postclose_update
        log = logging.getLogger("push")
        res = asyncio.run(job_postclose_update(
            s, period=args.period, dry_run=args.dry_run, logger=log,
            check_calendar=not args.no_calendar))
        print("=== 作业B[盘后更新] 结果 ===")
        print(res)
        return 0 if res.get("status") in ("ok", "skip") else 1
    if args.daemon:
        import logging
        import signal
        from push.state import State
        from push.scheduler import Scheduler
        log = logging.getLogger("push")
        state = State(s.state_dir)
        sched = Scheduler(s, state, log, tick_sec=args.tick)
        print(f"推送调度器已启动（tick={args.tick}s，state={s.state_dir}）")
        print(f"作业槽位：{sched.slots()}")
        print("按 Ctrl+C 或 systemctl stop 停止")

        async def _run():
            loop = asyncio.get_running_loop()
            # systemd stop 发 SIGTERM → 优雅停止；Windows 不支持则退回 KeyboardInterrupt
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, sched.stop)
                except (NotImplementedError, RuntimeError, AttributeError):
                    pass
            await sched.run_forever()

        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            sched.stop()
        print("调度器已停止")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
