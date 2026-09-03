# -*- coding: utf-8 -*-
"""推送系统独立配置

所有可调项集中在此，默认值即设计文档 §8 的推荐值；密钥（钉钉 webhook / 加签
secret）**只从环境变量读**，仓库里绝不硬编码。为方便本地 `--test-push`，内置一个
零依赖的极简 .env 加载器（默认找 deploy/push.env 或 .env，可用 PUSH_ENV_FILE 指定）。

环境变量命名：通用项以 PUSH_ 前缀，渠道密钥以 DINGTALK_ / WEBHOOK_ 前缀。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

# 项目根（push/ 的上一级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 极简 .env 加载（零依赖）：KEY=VALUE 每行一条，# 开头为注释，忽略空行。
# 已存在于 os.environ 的键不覆盖——systemd/docker 注入的真实环境优先于文件。
# ---------------------------------------------------------------------------
def _load_dotenv(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        # .env 读不到不应阻断启动：真实部署用 systemd/docker 注入环境变量
        pass


def _default_env_file() -> str:
    return os.environ.get("PUSH_ENV_FILE") or os.path.join(BASE_DIR, "deploy", "push.env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = (os.environ.get(key, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_list(key: str, default: List[str]) -> List[str]:
    v = (os.environ.get(key, "") or "").strip()
    if not v:
        return list(default)
    return [x.strip() for x in v.split(",") if x.strip()]


def _env_params(key: str) -> Dict[str, object]:
    """解析 'k=v,k=v' 为参数字典；值依次尝试 int→float→str（同 backtest.py 风格）。"""
    raw = (os.environ.get(key, "") or "").strip()
    out: Dict[str, object] = {}
    if not raw:
        return out
    for part in raw.split(","):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if not k:
            continue
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# 周期配置：日 K 开启、30m 预留。period 是一等维度，上线新周期只改这里 + 补数据。
#   enabled : 是否启用该周期的扫描推送
#   rule    : 买入战法 key（对应 app/backtest/rules.py 的 BUY_REGISTRY）
#   mode    : live=盘中合成当日末根判定 / close=只判已收盘末根（30m 用 close）
#   times   : 触发时刻列表（24h，"HH:MM"）；日 K 盘中一次，30m 每根收盘一次
# ---------------------------------------------------------------------------
DEFAULT_PERIODS: Dict[str, dict] = {
    "daily": {
        "enabled": True,
        "rule": "accumulation_detect",
        "mode": "live",
        "times": ["14:00"],
        "params": {"min_conditions": 4},   # 四条件全中(含低位≤35%)才推，去掉噪音
        "top_n": 10,                        # 按建仓强度排序只推前10
    },
    "30m": {
        "enabled": False,   # 预留：本期不实现
        "rule": "accumulation_detect",
        "mode": "close",
        "times": ["10:00", "10:30", "11:30", "14:00", "14:30", "15:00"],
        "params": {},
        "top_n": 0,
    },
}


@dataclass
class Settings:
    # --- 渠道 ---
    channel: str = "dingtalk"                 # dingtalk | webhook（预留）
    # 钉钉（密钥，仅环境变量）
    dingtalk_webhook: str = ""                # 机器人 webhook 完整 URL
    dingtalk_secret: str = ""                 # 加签 secret（SEC 开头）
    dingtalk_keyword: str = ""                # 可选：叠加自定义关键词
    # 通用 webhook（预留）
    webhook_url: str = ""
    webhook_token: str = ""

    # --- 推送行为 ---
    push_max_retries: int = 3                 # 发送失败重试次数
    push_retry_backoff: float = 2.0           # 重试退避基数（秒），指数退避
    http_timeout: float = 8.0                 # 单次 HTTP 超时

    # --- 作业 A：盘中扫描 ---
    scan_start: str = "14:00"                 # 兼容/缺省触发时刻（各周期 times 优先）
    scan_budget_sec: int = 120                # 有界扫描硬截止（秒）
    gather_concurrency: int = 10              # 数据抓取并发
    min_coverage: float = 0.6                 # 覆盖率兜底阈值，低于则推降级告警

    # --- 作业 B：盘后更新 ---
    postclose_first: str = "15:40"            # 盘后首跑时刻
    postclose_retries: List[str] = field(
        default_factory=lambda: ["16:40", "18:00", "20:00"])

    # --- 周期 ---
    periods: Dict[str, dict] = field(default_factory=lambda: dict(DEFAULT_PERIODS))

    # --- 日志/状态 ---
    log_level: str = "INFO"
    log_dir: str = field(default_factory=lambda: os.path.join(BASE_DIR, "push", "logs"))
    state_dir: str = field(default_factory=lambda: os.path.join(BASE_DIR, "push", "state"))

    @property
    def has_dingtalk(self) -> bool:
        # 加签模式(secret) 或 关键词模式(keyword) 二选一即可
        return bool(self.dingtalk_webhook and (self.dingtalk_secret or self.dingtalk_keyword))

    def enabled_periods(self) -> List[str]:
        return [p for p, cfg in self.periods.items() if cfg.get("enabled")]


def load_settings() -> Settings:
    """从环境变量（含可选 .env 文件）装配 Settings。"""
    _load_dotenv(_default_env_file())
    # 兼容：也尝试项目根 .env
    _load_dotenv(os.path.join(BASE_DIR, ".env"))

    s = Settings()
    s.channel = _env("PUSH_CHANNEL", s.channel).strip().lower()

    s.dingtalk_webhook = _env("DINGTALK_WEBHOOK", "").strip()
    s.dingtalk_secret = _env("DINGTALK_SECRET", "").strip()
    s.dingtalk_keyword = _env("DINGTALK_KEYWORD", "").strip()
    s.webhook_url = _env("WEBHOOK_URL", "").strip()
    s.webhook_token = _env("WEBHOOK_TOKEN", "").strip()

    s.push_max_retries = _env_int("PUSH_MAX_RETRIES", s.push_max_retries)
    s.push_retry_backoff = _env_float("PUSH_RETRY_BACKOFF", s.push_retry_backoff)
    s.http_timeout = _env_float("PUSH_HTTP_TIMEOUT", s.http_timeout)

    s.scan_start = _env("PUSH_SCAN_START", s.scan_start).strip()
    s.scan_budget_sec = _env_int("PUSH_SCAN_BUDGET_SEC", s.scan_budget_sec)
    s.gather_concurrency = _env_int("PUSH_GATHER_CONCURRENCY", s.gather_concurrency)
    s.min_coverage = _env_float("PUSH_MIN_COVERAGE", s.min_coverage)

    s.postclose_first = _env("PUSH_POSTCLOSE_FIRST", s.postclose_first).strip()
    s.postclose_retries = _env_list("PUSH_POSTCLOSE_RETRIES", s.postclose_retries)

    s.log_level = _env("PUSH_LOG_LEVEL", s.log_level).strip().upper()
    s.log_dir = _env("PUSH_LOG_DIR", s.log_dir).strip()
    s.state_dir = _env("PUSH_STATE_DIR", s.state_dir).strip()

    # 周期开关可用环境变量微调（不改默认结构）：
    #   PUSH_ENABLE_30M=true 开启 30m；PUSH_DAILY_TIMES="14:00,14:30" 覆盖触发时刻
    if _env_bool("PUSH_ENABLE_30M", False):
        s.periods["30m"]["enabled"] = True
    daily_times = _env_list("PUSH_DAILY_TIMES", [])
    if daily_times:
        s.periods["daily"]["times"] = daily_times
    daily_rule = _env("PUSH_DAILY_RULE", "").strip()
    if daily_rule:
        s.periods["daily"]["rule"] = daily_rule
    daily_params = _env_params("PUSH_DAILY_PARAMS")
    if daily_params:
        s.periods["daily"]["params"] = daily_params
    s.periods["daily"]["top_n"] = _env_int("PUSH_DAILY_TOPN", s.periods["daily"]["top_n"])

    return s


# 便捷单例（多数模块直接 from push.settings import settings）
settings = load_settings()
