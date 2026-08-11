# -*- coding: utf-8 -*-
"""行情数据源：腾讯实时快照 + 东方财富历史/增量K线 + 沪深300成分股 + BaoStock历史K线"""
import asyncio
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

TENCENT_URL = "https://qt.gtimg.cn/q="
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"

# 腾讯字段索引
T_IDX = {
    "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5, "volume_hand": 6,
    "time": 30, "change": 31, "change_pct": 32, "high": 33, "low": 34,
    "volume": 36, "amount_wan": 37, "turnover": 38, "pe": 39, "amplitude": 43,
    "float_mv": 44, "total_mv": 45, "pb": 46, "limit_up": 47, "limit_down": 48,
}


def code_to_market(code: str) -> Tuple[str, str]:
    """返回 (腾讯前缀, 东财secid)，如 ('sh', '1.600519')"""
    code = code.strip()
    if code.startswith(("4", "8", "92")):
        return "bj", f"0.{code}"
    if code.startswith(("60", "68", "90")):
        return "sh", f"1.{code}"
    if code.startswith(("00", "30", "20")):
        return "sz", f"0.{code}"
    return "sh", f"1.{code}"


def _f(s: str, idx: int, default: float = 0.0) -> float:
    """安全解析字段为浮点数（腾讯接口空字段为 ''）"""
    try:
        v = s[idx].strip()
        return float(v) if v else default
    except (IndexError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 腾讯实时快照
# ---------------------------------------------------------------------------

class TencentQuote:
    """腾讯行情批量快照接口（约3秒级刷新，免费无需鉴权）"""

    @staticmethod
    async def fetch(codes: List[str], client: Optional[httpx.AsyncClient] = None,
                    retries: int = 3) -> Dict[str, dict]:
        """批量拉取快照（失败自动重试）。返回 {code: snapshot_dict}，失败的股票不在结果中"""
        if not codes:
            return {}
        codes = list(dict.fromkeys(codes))
        symbols = ",".join(f"{code_to_market(c)[0]}{c}" for c in codes)
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
        for attempt in range(retries):
            try:
                if client is None:
                    async with httpx.AsyncClient(timeout=8.0, headers=headers) as c:
                        resp = await c.get(TENCENT_URL + symbols)
                        out = TencentQuote._parse(resp.text)
                else:
                    resp = await client.get(TENCENT_URL + symbols)
                    out = TencentQuote._parse(resp.text)
                if out:
                    return out
            except Exception:
                pass
            await asyncio.sleep(0.8 * (attempt + 1))
        return {}

    @staticmethod
    def _parse(text: str) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        # 形如 v_sh600519="1~贵州茅台~600519~...";
        for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
            symbol, payload = m.group(1), m.group(2)
            fields = payload.split("~")
            if len(fields) < 50:
                continue
            code = fields[T_IDX["code"]].strip()
            price = _f(fields, T_IDX["price"])
            if price <= 0:
                continue
            out[code] = {
                "code": code,
                "name": fields[T_IDX["name"]].strip(),
                "price": price,
                "prev_close": _f(fields, T_IDX["prev_close"]),
                "open": _f(fields, T_IDX["open"]),
                "high": _f(fields, T_IDX["high"]),
                "low": _f(fields, T_IDX["low"]),
                "change": _f(fields, T_IDX["change"]),
                "change_pct": _f(fields, T_IDX["change_pct"]),
                "volume": _f(fields, T_IDX["volume"]),          # 手
                "amount": _f(fields, T_IDX["amount_wan"]) * 1e4,  # 元
                "turnover": _f(fields, T_IDX["turnover"]),      # %
                "pe": _f(fields, T_IDX["pe"]) or 0.0,
                "pb": _f(fields, T_IDX["pb"]) or 0.0,
                "amplitude": _f(fields, T_IDX["amplitude"]),
                "float_mv": _f(fields, T_IDX["float_mv"]),      # 亿
                "total_mv": _f(fields, T_IDX["total_mv"]),      # 亿
                "limit_up": _f(fields, T_IDX["limit_up"]),
                "limit_down": _f(fields, T_IDX["limit_down"]),
                "time": fields[T_IDX["time"]].strip(),
            }
        return out


# ---------------------------------------------------------------------------
# 腾讯K线（备用数据源，东财受限时自动切换）
# ---------------------------------------------------------------------------

TENCENT_FQ_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_MK_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"

# 东财风格周期名 -> 腾讯风格周期名
TENCENT_PERIODS = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}


def _fmt_ts(ts: str) -> str:
    """腾讯时间戳 '202608101500' -> '2026-08-10 15:00'"""
    if len(ts) == 12 and ts.isdigit():
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}"
    return ts


class TencentKline:
    """腾讯K线接口：日K前复权 + 分钟K"""

    @staticmethod
    async def fetch_kline(
        code: str, period: str = "daily", limit: int = 300,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[dict]:
        prefix, _ = code_to_market(code)
        symbol = f"{prefix}{code}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
        if period == "daily":
            url, key = TENCENT_FQ_URL, "qfqday"
            params = {"param": f"{symbol},day,,,{limit},qfq"}
        elif period in TENCENT_PERIODS:
            mk = TENCENT_PERIODS[period]
            url, key = TENCENT_MK_URL, mk
            params = {"param": f"{symbol},{mk},,{limit}"}
        else:
            return []
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=8.0, headers=headers) as c:
                    return await TencentKline._request(c, url, params, symbol, key)
            return await TencentKline._request(client, url, params, symbol, key)
        except Exception:
            return []

    @staticmethod
    async def _request(client: httpx.AsyncClient, url: str, params: dict,
                       symbol: str, key: str) -> List[dict]:
        resp = await client.get(url, params=params)
        j = resp.json()
        data = (j.get("data") or {}).get(symbol) or {}
        rows = data.get(key) or data.get("day") or []
        out = []
        for row in rows:
            if len(row) < 6:
                continue
            try:
                out.append({
                    "ts": _fmt_ts(str(row[0])),
                    "open": float(row[1]),
                    "close": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "volume": float(row[5]),
                    "amount": 0.0,
                })
            except ValueError:
                continue
        return out


# ---------------------------------------------------------------------------
# 新浪K线（日K备用源：腾讯 fqkline 接口被WAF拦截时的兑底）
# ---------------------------------------------------------------------------

SINA_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


class SinaKline:
    """新浪K线：仅提供日K（scale=240）备用，不复权"""

    @staticmethod
    async def fetch_kline(
        code: str, period: str = "daily", limit: int = 300,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[dict]:
        if period != "daily":
            return []
        prefix, _ = code_to_market(code)
        symbol = f"{prefix}{code}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
        params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": limit}
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=8.0, headers=headers) as c:
                    return await SinaKline._request(c, params)
            return await SinaKline._request(client, params)
        except Exception:
            return []

    @staticmethod
    async def _request(client: httpx.AsyncClient, params: dict) -> List[dict]:
        resp = await client.get(SINA_KLINE_URL, params=params)
        rows = resp.json()
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            try:
                out.append({
                    "ts": _fmt_ts(str(row["day"])),
                    "open": float(row["open"]),
                    "close": float(row["close"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "volume": float(row["volume"]),
                    "amount": 0.0,
                })
            except (ValueError, KeyError, TypeError):
                continue
        return out


# ---------------------------------------------------------------------------
# BaoStock 历史K线（免费数据源：日K自1990年，分钟K自2020年，前复权）
# ---------------------------------------------------------------------------

# BaoStock 周期名映射
BS_PERIODS = {"5m": "5", "15m": "15", "30m": "30", "60m": "60",
              "daily": "d", "weekly": "w"}
BS_FIELDS_MIN = "date,time,open,high,low,close,volume,amount"
BS_FIELDS_DAY = "date,open,high,low,close,volume,amount"


class BaoStockKline:
    """BaoStock 历史K线：分钟K/日K/周K，前复权，区间查询
    同步 socket 库，通过 asyncio.to_thread 包装；懒登录 + 线程锁保护
    注意：baostock 底层为单连接，查询必须串行（_query_lock），并发请求排队执行
    分钟K volume 单位为股，换算为手（/100）与现有数据一致

    防挂起策略：锁获取超时 + 查询超时熔断（单次挂起即冷却 5 分钟），
    避免服务器风控时每个请求无限等待/反复重试拖垮整体"""

    _lock = threading.Lock()       # 登录锁
    _query_lock = threading.Lock() # 查询锁（单连接串行化）
    _ready = False
    # 熔断状态：查询/等锁超时视为服务器风控挂起，冷却期内直接跳过 BaoStock
    _cooldown_until = 0.0
    _cooldown_lock = threading.Lock()
    _LOCK_TIMEOUT = 25.0           # 等锁超时（秒）
    _QUERY_TIMEOUT = 20.0          # 单次查询超时（秒）
    _COOLDOWN_SECONDS = 300.0      # 熔断冷却（秒）

    @staticmethod
    def _available() -> bool:
        with BaoStockKline._cooldown_lock:
            return time.time() >= BaoStockKline._cooldown_until

    @staticmethod
    def _record_hang() -> None:
        """标记挂起：冷却期内不再尝试 BaoStock（风控信号）"""
        with BaoStockKline._cooldown_lock:
            BaoStockKline._cooldown_until = time.time() + BaoStockKline._COOLDOWN_SECONDS

    @staticmethod
    def _ensure_login():
        import baostock as bs
        if not BaoStockKline._lock.acquire(timeout=BaoStockKline._LOCK_TIMEOUT):
            raise RuntimeError("BaoStock 登录锁超时（疑似连接挂起）")
        try:
            if BaoStockKline._ready:
                return
            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
            BaoStockKline._ready = True
        finally:
            BaoStockKline._lock.release()

    @staticmethod
    def _symbol(code: str) -> str:
        if code.startswith(("4", "8", "92")):
            return f"bj.{code}"
        if code.startswith(("60", "68")):
            return f"sh.{code}"
        return f"sz.{code}"

    @staticmethod
    def _fetch_sync(code: str, period: str, limit: int, start_date: str,
                    end_date: str) -> List[dict]:
        freq = BS_PERIODS.get(period)
        if freq is None:   # 1m 等不支持周期
            return []
        try:
            BaoStockKline._ensure_login()
        except Exception:
            return []
        fields = BS_FIELDS_MIN if period.endswith("m") else BS_FIELDS_DAY
        if not start_date:
            # 未指定起始日期时按 limit 反推天数
            bpd = 8 if freq in ("5", "15", "30", "60") else 1
            start_date = (datetime.now() - timedelta(days=int(limit / bpd) + 10)).strftime("%Y-%m-%d")
        end = end_date or "2099-12-31"
        import baostock as bs
        # 等锁超时：锁被卡死线程占用视为风控挂起，放弃并触发熔断
        if not BaoStockKline._query_lock.acquire(timeout=BaoStockKline._LOCK_TIMEOUT):
            BaoStockKline._record_hang()
            return []
        try:
            try:
                # baostock 单连接：查询与遍历必须持有全局锁，否则并发响应错乱
                rs = bs.query_history_k_data_plus(
                    BaoStockKline._symbol(code), fields, start_date=start_date,
                    end_date=end, frequency=freq, adjustflag="2")
            except Exception:
                return []
            out: List[dict] = []
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                try:
                    if period.endswith("m"):
                        # time 形如 20240102100000000，取 HH:MM
                        ts = f"{row[0]} {row[1][8:10]}:{row[1][10:12]}"
                        o, c, h, l = float(row[2]), float(row[5]), float(row[4]), float(row[3])
                        volume = float(row[6]) / 100.0      # 股 -> 手
                        amount = float(row[7])
                    else:
                        ts = row[0]
                        o, c, h, l = float(row[1]), float(row[4]), float(row[3]), float(row[2])
                        volume = float(row[5]) / 100.0
                        amount = float(row[6])
                    out.append({"ts": ts, "open": o, "close": c,
                                "high": h, "low": l, "volume": volume, "amount": amount})
                except (ValueError, IndexError):
                    continue
            time.sleep(0.5)   # 节流（持锁期间）：降低连续查询触发限流的概率
            return out
        except Exception:
            return []
        finally:
            BaoStockKline._query_lock.release()

    @staticmethod
    async def fetch_kline(code: str, period: str = "daily", limit: int = 300,
                          start_date: str = "", end_date: str = "") -> List[dict]:
        """异步拉取（同步库在线程池执行，超时防单只查询挂起拖垮整体）
        冷却期内直接跳过；单次超时即触发熔断冷却"""
        if not BaoStockKline._available():
            return []
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(
                    BaoStockKline._fetch_sync, code, period, limit, start_date, end_date),
                timeout=BaoStockKline._QUERY_TIMEOUT)
            return data
        except asyncio.TimeoutError:
            BaoStockKline._record_hang()   # 查询挂起 = 风控信号，冷却期内不再尝试
            return []
        except Exception:
            return []


# ---------------------------------------------------------------------------
# 东方财富K线
# ---------------------------------------------------------------------------

# 东财周期参数：klt -> 周期名
PERIOD_KLINES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
    "daily": 101, "weekly": 102,
}

# 东财 klines 字段顺序（逗号分隔）
# 日期时间, 开, 收, 高, 低, 量, 额, 振幅, 涨跌幅, 涨跌额, 换手率

# 东财熔断：连续失败超过阈值后冷却一段时间不再尝试，避免每次请求都空等超时
EM_FAIL_THRESHOLD = 3
EM_COOLDOWN_SECONDS = 300.0
_em_fail_count = 0
_em_disabled_until = 0.0


def em_available() -> bool:
    return time.time() >= _em_disabled_until


def em_record(ok: bool):
    global _em_fail_count, _em_disabled_until
    if ok:
        _em_fail_count = 0
        return
    _em_fail_count += 1
    if _em_fail_count >= EM_FAIL_THRESHOLD:
        _em_disabled_until = time.time() + EM_COOLDOWN_SECONDS
        _em_fail_count = 0


class EastmoneyKline:
    """东方财富K线接口：历史拉取 + 增量同步"""

    @staticmethod
    async def fetch_kline(
        code: str, period: str = "daily", limit: int = 300,
        client: Optional[httpx.AsyncClient] = None, start_date: str = "",
    ) -> List[dict]:
        """拉取K线。返回 [{ts, open, close, high, low, volume, amount}]（按时间升序）
        东财失败时自动重试一次；连续失败触发熔断后由调用方改走腾讯源
        start_date: 可选起始日期 YYYY-MM-DD，替代按 limit 反推"""
        if not em_available():
            return []
        for attempt in range(2):
            data = await EastmoneyKline._fetch_once(code, period, limit, client, start_date)
            if data:
                em_record(True)
                return data
            await asyncio.sleep(0.5 * (attempt + 1))
        em_record(False)
        return []

    @staticmethod
    async def _fetch_once(
        code: str, period: str = "daily", limit: int = 300,
        client: Optional[httpx.AsyncClient] = None, start_date: str = "",
    ) -> List[dict]:
        klt = PERIOD_KLINES.get(period)
        if klt is None:
            return []
        # beg 指定起始日期；否则按需拉取最近若干天
        if start_date:
            beg_date = start_date.replace("-", "")
        else:
            days = limit * 2 if klt >= 101 else limit // 4 + 1
            beg_date = (time.strftime("%Y%m%d", time.localtime(time.time() - days * 86400)))
        _, secid = code_to_market(code)
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": klt, "fqt": 1, "beg": beg_date, "end": 20500101, "lmt": limit,
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=4.0, headers=headers) as c:
                    resp = await c.get(EM_KLINE_URL, params=params)
                    return EastmoneyKline._parse(resp.json())
            resp = await client.get(EM_KLINE_URL, params=params, timeout=4.0)
            return EastmoneyKline._parse(resp.json())
        except Exception:
            return []

    @staticmethod
    def _parse(payload: dict) -> List[dict]:
        klines = (payload or {}).get("data") or {}
        raw = klines.get("klines") or []
        out = []
        for line in raw:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                out.append({
                    "ts": parts[0],                       # "2025-08-10" 或 "2025-08-10 09:31"
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),            # 手
                    "amount": float(parts[6]),            # 元
                })
            except ValueError:
                continue
        return out


# ---------------------------------------------------------------------------
# 东财搜索建议（代码/名称/拼音）
# ---------------------------------------------------------------------------

EM_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
EM_SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"


class EastmoneySuggest:
    """东财搜索建议：按代码/名称/拼音搜索A股（沪深+北交所），返回 [(code, name)]"""

    @staticmethod
    async def search(q: str, client: Optional[httpx.AsyncClient] = None) -> List[Tuple[str, str]]:
        params = {"input": q, "type": 14, "token": EM_SUGGEST_TOKEN}
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=8.0, headers=headers) as c:
                    resp = await c.get(EM_SUGGEST_URL, params=params)
            else:
                resp = await client.get(EM_SUGGEST_URL, params=params)
            data = ((resp.json() or {}).get("QuotationCodeTable") or {}).get("Data") or []
            out = []
            for d in data:
                if d.get("Classify") not in ("AStock", "NEEQ"):
                    continue
                code = str(d.get("Code") or "").strip()
                name = str(d.get("Name") or "").strip()
                if code and name:
                    out.append((code, name))
            return out
        except Exception:
            return []


# ---------------------------------------------------------------------------
# 沪深300成分股（可选股票池来源）
# ---------------------------------------------------------------------------

class EastmoneyPool:
    """从东方财富拉取板块成分股列表"""

    @staticmethod
    async def fetch_hs300(client: Optional[httpx.AsyncClient] = None) -> List[Tuple[str, str]]:
        """拉取沪深300成分股，返回 [(code, name), ...]"""
        return await EastmoneyPool.fetch_board("b:BK0500", client=client)

    @staticmethod
    async def fetch_board(fs: str, client: Optional[httpx.AsyncClient] = None) -> List[Tuple[str, str]]:
        params = {
            "pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": fs, "fields": "f12,f14",
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=8.0, headers=headers) as c:
                    resp = await c.get(EM_CLIST_URL, params=params)
                    return EastmoneyPool._parse(resp.json())
            resp = await client.get(EM_CLIST_URL, params=params)
            return EastmoneyPool._parse(resp.json())
        except Exception:
            return []

    @staticmethod
    def _parse(payload: dict) -> List[Tuple[str, str]]:
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        return [(str(x.get("f12")), str(x.get("f14"))) for x in diff if x.get("f12")]

    @staticmethod
    async def fetch_all(client: Optional[httpx.AsyncClient] = None) -> List[Tuple[str, str]]:
        """全市场A股列表（沪深主板/创业板/科创板，不含北交所），分页拉取"""
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        out: List[Tuple[str, str]] = []
        for pn in range(1, 16):
            params = {
                "pn": pn, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12", "fs": fs, "fields": "f12,f14",
            }
            try:
                if client is None:
                    async with httpx.AsyncClient(timeout=8.0, headers=headers) as c:
                        resp = await c.get(EM_CLIST_URL, params=params)
                        page = EastmoneyPool._parse(resp.json())
                else:
                    resp = await client.get(EM_CLIST_URL, params=params, timeout=8.0)
                    page = EastmoneyPool._parse(resp.json())
            except Exception:
                break
            if not page:
                break
            out.extend(page)
            if len(page) < 500:
                break
        return out


# ---------------------------------------------------------------------------
# 统一异步客户端（共享连接池）
# ---------------------------------------------------------------------------

class DataSource:
    """共享 HTTP 客户端 + 常用操作封装"""

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=8.0,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        self._lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def snapshots(self, codes: List[str]) -> Dict[str, dict]:
        return await TencentQuote.fetch(codes, client=self.client)

    async def kline(self, code: str, period: str = "daily", limit: int = 300,
                    min_len: int = 0, start_date: str = "", end_date: str = "") -> List[dict]:
        """K线获取：东财 → 腾讯 → 新浪 → BaoStock 逐级降级。
        min_len：期望返回根数，某级返回不足时继续降级（末级返回已有数据）
        start_date/end_date：可选精确区间（YYYY-MM-DD），供长历史回测使用"""
        # 惰性创建协程：逐级尝试，命中即停，避免未执行的协程产生警告
        fetchers = [
            lambda: EastmoneyKline.fetch_kline(code, period, limit, client=self.client,
                                               start_date=start_date),
            lambda: TencentKline.fetch_kline(code, period, limit, client=self.client),
            lambda: SinaKline.fetch_kline(code, period, limit, client=self.client),
            lambda: BaoStockKline.fetch_kline(code, period, limit,
                                              start_date=start_date, end_date=end_date),
        ]
        for i, make in enumerate(fetchers):
            data = await make()
            if data and (len(data) >= min_len or i == len(fetchers) - 1):
                return data
            if i == len(fetchers) - 1:
                return data or []
        return []

    async def hs300(self) -> List[Tuple[str, str]]:
        return await EastmoneyPool.fetch_hs300(client=self.client)

    async def all_stocks(self) -> List[Tuple[str, str]]:
        return await EastmoneyPool.fetch_all(client=self.client)

    async def search(self, q: str) -> List[Tuple[str, str]]:
        """按代码/名称/拼音搜索股票（沪深+北交所），失败返回空列表"""
        return await EastmoneySuggest.search(q, client=self.client)

    async def fetch_many_kline(self, codes: List[str], period: str, limit: int,
                               concurrency: int = 10, min_len: int = 0,
                               start_date: str = "", end_date: str = "") -> Dict[str, List[dict]]:
        """并发拉取多只股票K线"""
        sem = asyncio.Semaphore(concurrency)
        results: Dict[str, List[dict]] = {}

        async def one(code: str):
            async with sem:
                data = await self.kline(code, period, limit, min_len=min_len,
                                        start_date=start_date, end_date=end_date)
                if data:
                    results[code] = data

        await asyncio.gather(*(one(c) for c in codes))
        return results
