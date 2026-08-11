# -*- coding: utf-8 -*-
"""选股系统后端：FastAPI + WebSocket 实时推送 + 静态前端"""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
from app import patterns
from app.datasource import DataSource
from app.scanner import Scanner, is_trading_time
from app.store import Store
from app.strategies import StrategyEngine

STATIC_DIR = os.path.join(config.BASE_DIR, "static")


class ConnectionManager:
    """WebSocket 连接管理与事件广播"""

    def __init__(self):
        self.connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.connections.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.connections.discard(ws)

    async def broadcast(self, event: dict):
        payload = json.dumps(event, ensure_ascii=False)
        dead = []
        async with self._lock:
            for ws in list(self.connections):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

store = Store()
ds = DataSource()
engine = StrategyEngine()
manager = ConnectionManager()
scanner: Scanner = None
kline_cache: dict = {}          # (code, period) -> (expire_ts, data)
KLINE_CACHE_TTL = 20.0
scan_cache: dict = {}           # (pattern, period, window, day, scope) -> (ts, result)
SCAN_CACHE_TTL = 30.0
SCAN_CACHE_TTL_ALL = 300.0      # 全市场扫描成本高，缓存更久
all_stocks_cache = {"ts": 0.0, "data": []}   # 全市场股票列表缓存（1小时）

# 分钟周期与东财参数的映射（用于按需拉取K线图数据）
MIN_PERIODS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner
    scanner = Scanner(store, ds, engine, manager.broadcast)
    await scanner.start()
    yield
    await scanner.stop()


app = FastAPI(title="实时选股系统", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# REST 接口
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/status")
async def status():
    return {
        "trading": scanner is not None and scanner._running,
        "trading_time": is_trading_time(),
        "pool_size": len(store.get_stocks()),
        "snapshot_count": len(scanner.snapshots) if scanner else 0,
        "strategies": engine.list(),
        "last_snapshot_at": scanner.last_snapshot_at if scanner else 0,
        "last_kline_sync_at": scanner.last_kline_sync_at if scanner else 0,
        "uptime": int(time.time() - scanner.started_at) if scanner else 0,
    }


@app.get("/api/snapshots")
async def snapshots():
    return {"snapshots": list(scanner.snapshots.values()) if scanner else []}


@app.get("/api/pool")
async def pool():
    return {"stocks": [{"code": c, "name": n} for c, n in store.get_stocks()]}


@app.post("/api/pool")
async def add_pool(req: dict):
    code = str(req.get("code", "")).strip()
    if not code:
        return {"ok": False, "msg": "缺少股票代码"}
    snap = (await ds.snapshots([code])).get(code)
    if not snap:
        return {"ok": False, "msg": f"代码 {code} 行情获取失败，请确认是有效的A股代码"}
    store.upsert_stocks([(code, snap.get("name") or code)])
    # 立即拉取该股票K线并加入缓存
    daily = await ds.kline(code, "daily", config.DAILY_HISTORY_LIMIT)
    store.upsert_klines(code, daily, "kline_daily")
    scanner.daily_cache[code] = store.get_klines(code, "kline_daily", 400)
    return {"ok": True, "msg": f"已添加 {snap.get('name')} {code} 到股票池"}


@app.delete("/api/pool/{code}")
async def remove_pool(code: str):
    store.remove_stock(code)
    scanner.snapshots.pop(code, None)
    scanner.daily_cache.pop(code, None)
    scanner.k60_cache.pop(code, None)
    return {"ok": True, "msg": f"已移除 {code}"}


@app.get("/api/watch")
async def get_watch():
    return {"watch": [{"code": c, "name": n} for c, n in store.get_watch()]}


@app.post("/api/watch")
async def add_watch(req: dict):
    code = str(req.get("code", "")).strip()
    if not code:
        return {"ok": False, "msg": "请输入股票代码"}
    snap = (await ds.snapshots([code])).get(code)
    if not snap:
        return {"ok": False, "msg": f"代码 {code} 行情获取失败，请确认是有效的A股代码"}
    store.add_watch(code, snap.get("name") or code)
    return {"ok": True, "msg": f"已加入自选：{snap.get('name')} {code}"}


@app.delete("/api/watch/{code}")
async def remove_watch(code: str):
    store.remove_watch(code)
    return {"ok": True, "msg": f"已移出自选：{code}"}


@app.get("/api/kline")
async def kline(code: str, period: str = "daily", limit: int = 250):
    """K线图数据：优先本地库，分钟周期按需从东财实时拉取"""
    if period in MIN_PERIODS:
        cache_key = (code, period)
        now = time.time()
        hit = kline_cache.get(cache_key)
        if hit and now - hit[0] < KLINE_CACHE_TTL:
            return {"code": code, "period": period, "klines": hit[1]}
        data = await ds.kline(code, period, max(limit, 300))
        if data:
            store.upsert_klines(code, data, "kline_min")
            kline_cache[cache_key] = (now, data)
        return {"code": code, "period": period, "klines": data}
    data = store.get_klines(code, "kline_daily", limit)
    return {"code": code, "period": period, "klines": data}


@app.get("/api/signals")
async def signals(limit: int = 100, code: str = ""):
    """信号列表，可按股票代码过滤（用于K线图信号标注）"""
    return {"signals": store.get_signals(limit, code or None)}


@app.get("/api/patterns")
async def list_patterns():
    return {"patterns": [{"key": k, "name": v["name"], "desc": v["desc"]}
                         for k, v in patterns.PATTERNS.items()]}


@app.get("/api/scan")
async def scan_pattern(pattern: str, period: str = "30m", window: int = 5,
                       day: str = "latest", scope: str = "pool"):
    """K线形态扫描，如：30分K最后5根出现阳包阴
    day: latest=最近有数据的交易日, today=仅今天, prev=前一交易日
    scope: pool=仅监控股票池, all=全市场（首次扫描约半分钟）"""
    if pattern not in patterns.PATTERNS:
        return {"ok": False, "msg": "未知形态"}
    cache_key = (pattern, period, window, day, scope)
    now = time.time()
    ttl = SCAN_CACHE_TTL_ALL if scope == "all" else SCAN_CACHE_TTL
    hit = scan_cache.get(cache_key)
    if hit and now - hit[0] < ttl:
        return hit[1]

    start = time.time()
    if scope == "all":
        # 全市场列表：优先缓存，失效则从东财重新拉取
        if time.time() - all_stocks_cache["ts"] > 3600:
            fetched = await ds.all_stocks()
            if fetched:
                all_stocks_cache["ts"] = time.time()
                all_stocks_cache["data"] = fetched
        stocks = list(all_stocks_cache["data"])
        if not stocks:
            result = {"ok": False, "msg": "获取全市场股票列表失败（数据源限流），请稍后重试或改用股票池范围",
                      "matches": [], "elapsed_ms": int((time.time() - start) * 1000)}
            return result
    else:
        stocks = store.get_stocks()
    codes = [c for c, _ in stocks]
    names = dict(stocks)
    concurrency = 30 if scope == "all" else 10
    data = await ds.fetch_many_kline(codes, period, limit=40, concurrency=concurrency)

    all_dates = sorted({k["ts"][:10] for kl in data.values() for k in kl})
    today = time.strftime("%Y-%m-%d")
    if day == "today":
        target = today if today in all_dates else None
    elif day == "prev":
        target = all_dates[-2] if len(all_dates) >= 2 else (all_dates[-1] if all_dates else None)
    else:
        target = all_dates[-1] if all_dates else None

    result = {"ok": True, "pattern": pattern, "period": period, "window": window,
              "scope": scope, "date": target, "matches": [], "scanned": len(codes),
              "fetched": len(data), "elapsed_ms": 0}
    if target is None:
        result["msg"] = "今天尚无K线数据（未开盘或刚开盘）"
        result["elapsed_ms"] = int((time.time() - start) * 1000)
        scan_cache[cache_key] = (now, result)
        return result

    for code, klines in data.items():
        day_k = [k for k in klines if k["ts"][:10] == target]
        hits = patterns.scan(day_k, pattern, window)
        if hits:
            snap = scanner.snapshots.get(code, {}) if scanner else {}
            # 形态触发前的趋势背景（取最早一次触发之前的 lookback 根，可跨日）
            trend, trend_pct = patterns.trend_before(klines, hits[0]["ts"])
            result["matches"].append({
                "code": code,
                "name": snap.get("name") or names.get(code, code),
                "price": snap.get("price") or (day_k[-1]["close"] if day_k else None),
                "change_pct": snap.get("change_pct"),
                "trend": trend,
                "trend_pct": trend_pct,
                "hits": hits,
            })
    result["matches"].sort(
        key=lambda m: (m["hits"][-1]["ts"], m.get("change_pct") or 0), reverse=True)
    result["elapsed_ms"] = int((time.time() - start) * 1000)
    scan_cache[cache_key] = (now, result)
    return result


@app.get("/api/strategies")
async def strategies():
    return {"strategies": engine.list()}


@app.post("/api/strategies/toggle")
async def toggle_strategy(req: dict):
    key = str(req.get("key", ""))
    enabled = bool(req.get("enabled"))
    ok = engine.toggle(key, enabled)
    return {"ok": ok, "msg": f"{key} -> {'启用' if enabled else '停用'}" if ok else "未知策略"}


# ---------------------------------------------------------------------------
# WebSocket 实时推送
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # 连接建立后立即推送初始快照与信号
    try:
        if scanner:
            await ws.send_text(json.dumps({
                "type": "snapshots",
                "data": {"snapshots": list(scanner.snapshots.values()),
                         "trading": True, "ts": time.time()},
            }, ensure_ascii=False))
        await ws.send_text(json.dumps({
            "type": "signals_history",
            "data": {"signals": store.get_signals(100)},
        }, ensure_ascii=False))
        await ws.send_text(json.dumps({
            "type": "status",
            "data": {"msg": "连接成功", "strategies": engine.list(),
                     "trading": is_trading_time()},
        }, ensure_ascii=False))
        while True:
            msg = await ws.receive_text()  # 保持连接，客户端一般不发消息
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
