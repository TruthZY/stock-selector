# -*- coding: utf-8 -*-
"""用户自定义战法脚本加载器（config.USER_RULES_DIR 下的 *.py）

用户脚本就是一个 BuyRule / SellRule 子类 + @register_buy/@register_sell，
和内置规则完全同构，因此自动获得：验证台参数表单（default_params +
PARAM_LABELS + PARAM_META）、prepare() 预计算钩子、与内置规则自由组合、CLI 可用。

三条设计约束：

1. **不接收上传的代码。** 脚本从磁盘目录加载。能写这个目录的人本来就能执行代码，
   所以加载器不增加任何权限；而一个 HTTP 上传接口会把"能访问端口"升级成
   "能执行任意代码"。重载端点只是重新读磁盘，同理不引入新的执行面。

2. **每个文件独立兜住异常。** 一个脚本语法错误不能影响别的脚本，更不能让服务起不来。

3. **性能门禁。** 新手最常见的写法是在 on_bar 里直接调 conditions.*，那些函数每次
   都在 bars[:i+1] 上重算整段指标，是 O(n²)。实测单个条件跑完 5048 根要 8.5 秒，
   全池验证会变成两小时。所以注册后立刻实测，超预算的直接不予注册。
"""
import importlib.util
import os
import time
import traceback
from typing import Dict, List, Optional, Tuple

import config
from app.backtest.rules import BUY_REGISTRY, SELL_REGISTRY
from app.backtest.strategy import BarContext

# 文件 → 该文件注册的 (registry, key)，重载时据此清掉已删除/改名的规则
_loaded_keys: Dict[str, List[Tuple[dict, str]]] = {}
# 最近一次加载报告，供 API / CLI 展示
_last_report: dict = {}


def last_report() -> dict:
    """最近一次加载报告（未加载过则为空 dict）"""
    return dict(_last_report)


# ---------------------------------------------------------------------------
# 性能门禁
# ---------------------------------------------------------------------------

def _sample_bars() -> List[dict]:
    """取本地缓存里最长的一条K线序列作为门禁样本；无缓存返回空列表"""
    try:
        from app.backtest.cache import KlineCache
        import sqlite3
        conn = sqlite3.connect(f"file:{config.DB_PATH}?immutable=1", uri=True)
        try:
            row = conn.execute(
                "SELECT code, period, COUNT(*) n FROM kline_cache "
                "GROUP BY code, period ORDER BY n DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if not row:
            return []
        bars = KlineCache().get_all(row[0], row[1]) or []
        return bars[-config.USER_RULE_SAMPLE_BARS:]
    except Exception:
        return []


def _is_sell_rule(cls) -> bool:
    from app.backtest.rules import SellRule
    return isinstance(cls, type) and issubclass(cls, SellRule)


def benchmark_rule(cls, bars: List[dict]) -> dict:
    """实测一个规则跑完整序列的耗时，归一化到 USER_RULE_SAMPLE_BARS 根

    返回 {ok, ms, norm_ms, bars, budget, error}
    ok=False 表示超预算或脚本抛异常——两者都算性能/正确性不合格
    """
    budget = float(config.USER_RULE_BUDGET_MS)
    n = len(bars)
    if n < 60:
        # 样本不足无法测量。"测不了"不等于"合格"，但也不该因此拒绝注册，
        # 只在报告里注明，等有数据后重载再测
        return {"ok": True, "ms": None, "norm_ms": None, "bars": n,
                "budget": budget, "error": "", "skipped": True}
    try:
        rule = cls()
        rule.reset()
        # 卖出规则的契约是"持仓中调用"，喂 position=None 会让它在读 buy_price 时
        # 报错而被误判不合格。这里造一个合成持仓：买价取样本首根收盘、
        # 入场索引 0，使规则能走完全部分支（吊灯止损等还会用到 on_position_opened）
        pos = None
        if _is_sell_rule(cls):
            from app.backtest.position import Position
            pos = Position(code="__bench__", name="", buy_ts=bars[0]["ts"],
                           buy_price=bars[0]["close"], shares=100.0,
                           cost=bars[0]["close"] * 100.0, entry_bar_index=0)
        t0 = time.perf_counter()
        rule.prepare(bars)
        if pos is not None:
            rule.on_position_opened(
                BarContext(code="__bench__", name="", bars=bars, i=0,
                           position=pos, params=rule.params), pos)
        for i in range(1, n):
            rule.on_bar(BarContext(code="__bench__", name="", bars=bars, i=i,
                                   position=pos, params=rule.params))
        ms = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        return {"ok": False, "ms": None, "norm_ms": None, "bars": n,
                "budget": budget, "skipped": False,
                "error": f"{type(e).__name__}: {e}"}
    norm = ms / n * config.USER_RULE_SAMPLE_BARS
    return {"ok": norm <= budget, "ms": round(ms, 1), "norm_ms": round(norm, 1),
            "bars": n, "budget": budget, "error": "", "skipped": False}


SLOW_HINT = ("指标要在 prepare() 里算一次并缓存成序列，on_bar() 只做 O(1) 比较。"
             "在 on_bar 里直接调用 conditions.* 会每根K线重算整段指标（O(n²)）")


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def _purge(path: str) -> None:
    """清掉某个文件此前注册的规则（重载时用）"""
    for reg, key in _loaded_keys.pop(path, []):
        reg.pop(key, None)


def _import_file(path: str) -> None:
    """按文件路径导入模块。每次用唯一模块名，避免 sys.modules 缓存导致改动不生效"""
    name = f"_user_rule_{abs(hash(path))}_{int(time.time() * 1000)}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def load_user_rules(sample: Optional[List[dict]] = None) -> dict:
    """扫描并加载用户脚本，返回加载报告

    报告：{dir, files, loaded[], failed[{file,error}], slow[{key,...}], sample_bars}
    """
    global _last_report
    directory = config.USER_RULES_DIR
    report = {"dir": directory, "files": 0, "loaded": [], "failed": [],
              "slow": [], "sample_bars": 0}
    if not os.path.isdir(directory):
        _last_report = report
        return report

    files = sorted(f for f in os.listdir(directory)
                   if f.endswith(".py") and not f.startswith("_"))
    report["files"] = len(files)
    # 先把上一轮注册的全部摘掉再重新导入：只在"文件还在"时 purge 的话，
    # 脚本被删除/改名后它注册的规则会一直留在注册表里
    for path in list(_loaded_keys):
        _purge(path)
    if sample is None:
        sample = _sample_bars()
    report["sample_bars"] = len(sample)

    for fname in files:
        path = os.path.join(directory, fname)
        _purge(path)
        before = ({k: v for k, v in BUY_REGISTRY.items()},
                  {k: v for k, v in SELL_REGISTRY.items()})
        try:
            _import_file(path)
        except Exception as e:
            # 单个脚本出错不影响其他脚本，也不影响服务启动
            tb = traceback.format_exc().strip().splitlines()
            report["failed"].append({
                "file": fname, "error": f"{type(e).__name__}: {e}",
                "where": tb[-2].strip() if len(tb) >= 2 else ""})
            continue

        # 找出这个文件新注册了哪些 key；顶掉已有 key 视为冲突并还原，
        # 否则内置规则会被悄悄替换，且重载时清理逻辑会把内置的一起删掉
        added: List[Tuple[dict, str, str]] = []
        conflicts: List[str] = []
        for reg, kind in ((BUY_REGISTRY, "buy"), (SELL_REGISTRY, "sell")):
            old = before[0] if reg is BUY_REGISTRY else before[1]
            for key in list(reg):
                if key not in old:
                    added.append((reg, key, kind))
                elif reg[key] is not old[key]:
                    reg[key] = old[key]          # 还原被顶掉的规则
                    conflicts.append(f"{kind}:{key}")
        if conflicts:
            report["failed"].append({
                "file": fname, "where": "",
                "error": f"规则 key 与已有规则冲突：{', '.join(conflicts)}（请改名）"})
        if not added:
            if not conflicts:       # 冲突已单独报过，不再叠一条"未注册"
                report["failed"].append({
                    "file": fname, "where": "",
                    "error": "未注册任何规则（是否忘了 @register_buy / @register_sell？）"})
            continue

        keep: List[Tuple[dict, str]] = []
        for reg, key, kind in added:
            bench = benchmark_rule(reg[key], sample)
            entry = {"file": fname, "key": key, "kind": kind,
                     "name": getattr(reg[key], "name", "") or key,
                     "ms": bench["norm_ms"], "budget": bench["budget"],
                     "bars": bench["bars"]}
            if bench["ok"]:
                if bench.get("skipped"):
                    entry["note"] = "本地缓存不足，未做性能实测"
                report["loaded"].append(entry)
                keep.append((reg, key))
            else:
                # 不合格：直接摘掉注册，不让它出现在验证台里
                reg.pop(key, None)
                entry["error"] = bench["error"]
                entry["hint"] = SLOW_HINT if not bench["error"] else ""
                report["slow"].append(entry)
        _loaded_keys[path] = keep

    _last_report = report
    return report
