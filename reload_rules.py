# -*- coding: utf-8 -*-
"""重新加载 user_rules/ 下的自定义战法脚本：python reload_rules.py

两步：
1. 本地加载 + 性能门禁，打印报告。服务没起也能用来校验脚本；
   有脚本加载失败或性能不合格时退出码为 1，方便串到别的流程里
2. 若服务正在运行，再调 /api/backtest/rules/reload，让运行中的进程也换上新脚本
   （只让服务重读磁盘上已有的文件，不上传任何代码）
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from app.backtest.rules import reload_user_rules

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BAR = "=" * 60
SERVER = "http://127.0.0.1:8000"


def _print_report(r: dict) -> None:
    print(f"目录: {r.get('dir')}")
    print(f"扫描 {r.get('files', 0)} 个 .py 文件"
          + (f"，性能门禁样本 {r['sample_bars']} 根" if r.get("sample_bars")
             else "，本地无缓存数据、跳过性能实测"))
    print()

    loaded = r.get("loaded") or []
    if loaded:
        print(f"✓ 已加载 {len(loaded)} 个规则：")
        for e in loaded:
            ms = f"{e['ms']:.1f}ms" if e.get("ms") is not None else "未实测"
            kind = "买入" if e["kind"] == "buy" else "卖出"
            print(f"    [{kind}] {e['key']:<24} {e['name']:<20} 耗时 {ms:>9}"
                  + (f"  ({e['note']})" if e.get("note") else ""))
        print()

    slow = r.get("slow") or []
    if slow:
        print(f"✗ 性能/运行不合格，未予注册 {len(slow)} 个：")
        for e in slow:
            print(f"    [{e['file']}] {e['key']}")
            if e.get("error"):
                print(f"        运行出错: {e['error']}")
            else:
                print(f"        实测 {e['ms']:.0f}ms / 预算 {e['budget']:.0f}ms"
                      f"（按 {config.USER_RULE_SAMPLE_BARS} 根归一化）")
            if e.get("hint"):
                print(f"        {e['hint']}")
        print()

    failed = r.get("failed") or []
    if failed:
        print(f"✗ 加载失败 {len(failed)} 个文件：")
        for e in failed:
            print(f"    [{e['file']}] {e['error']}")
            if e.get("where"):
                print(f"        {e['where']}")
        print()

    if not loaded and not slow and not failed:
        print("（没有找到任何自定义战法脚本）")
        print(f"提示：把 .py 放进 {r.get('dir')}，可参考里面的 example_ma_cross.py")


def _notify_server() -> None:
    """服务在跑就让它也重载；没跑就跳过（不是错误）"""
    try:
        with urllib.request.urlopen(f"{SERVER}/api/status", timeout=3):
            pass
    except Exception:
        print("服务未运行，本次仅做本地校验（下次启动服务会自动加载）")
        return
    try:
        req = urllib.request.Request(f"{SERVER}/api/backtest/rules/reload",
                                     data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        n = len(r.get("loaded") or [])
        print(f"运行中的服务已重载：{n} 个规则生效，验证台刷新即可看到")
    except Exception as e:
        print(f"[警告] 通知服务重载失败：{type(e).__name__}: {e}")
        print("       本地校验结果仍然有效，重启服务也能生效")


def main() -> None:
    print(BAR)
    print("  重新加载自定义战法脚本")
    print(BAR)
    if not os.path.isdir(config.USER_RULES_DIR):
        print(f"目录不存在：{config.USER_RULES_DIR}")
        sys.exit(1)

    report = reload_user_rules()
    _print_report(report)
    _notify_server()
    print(BAR)

    bad = len(report.get("failed") or []) + len(report.get("slow") or [])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
