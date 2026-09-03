# -*- coding: utf-8 -*-
"""日志：结构化格式 + 文件轮转 + 密钥脱敏

脱敏是硬要求：webhook 的 access_token、钉钉加签 secret、签名 sign、timestamp 等
一旦进日志就等于泄露凭据。这里用一个 logging.Filter 在**每条记录落地前**做替换，
既按正则兜底常见模式，也把 settings 里已知的真实密钥字面量直接打码。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
from typing import Iterable, List

# 常见敏感模式（兜底，即使没登记真实密钥也能挡住）
_PATTERNS = [
    (re.compile(r"(access_token=)[0-9a-zA-Z]+"), r"\1***"),
    (re.compile(r"(SEC)[0-9a-fA-F]{16,}"), r"\1***"),          # 钉钉加签 secret
    (re.compile(r"(sign=)[^&\s]+"), r"\1***"),
    (re.compile(r"(timestamp=)\d+"), r"\1***"),
    (re.compile(r"(secret[\"']?\s*[:=]\s*[\"']?)[^\"',\s]+"), r"\1***"),
    (re.compile(r"(webhook[\"']?\s*[:=]\s*[\"']?)https?://\S+"), r"\1<redacted-url>"),
]


class RedactingFilter(logging.Filter):
    """把记录文本里的敏感串替换成掩码。"""

    def __init__(self, literals: Iterable[str] = ()):
        super().__init__()
        # 已知的真实密钥字面量（长度>=6 才登记，避免误伤短词）
        self._literals: List[str] = sorted(
            {s for s in literals if s and len(s) >= 6}, key=len, reverse=True)

    def _scrub(self, text: str) -> str:
        if not text:
            return text
        for lit in self._literals:
            if lit in text:
                text = text.replace(lit, "***")
        for pat, repl in _PATTERNS:
            text = pat.sub(repl, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = self._scrub(record.msg)
            if record.args:
                # 只对字符串参数脱敏，其余原样
                if isinstance(record.args, dict):
                    record.args = {k: self._scrub(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        self._scrub(a) if isinstance(a, str) else a for a in record.args)
        except Exception:
            # 脱敏失败也不能让日志崩掉业务；保守起见清空可能敏感的原文
            record.msg = "<redaction-error>"
            record.args = ()
        return True


_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_dir: str = "",
                  secrets: Iterable[str] = (), name: str = "push",
                  console: bool = True) -> logging.Logger:
    """配置并返回 push 日志器。

    level    : 日志级别
    log_dir  : 文件日志目录；为空则只输出到控制台
    secrets  : 需要额外打码的真实密钥字面量（webhook/secret 等）
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    # 幂等：重复调用先清空旧 handler，避免叠加导致重复输出
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    redactor = RedactingFilter(secrets)
    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(redactor)
        logger.addHandler(sh)

    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, f"{name}.log"),
                maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.addFilter(redactor)
            logger.addHandler(fh)
        except OSError:
            logger.warning("文件日志初始化失败，仅用控制台输出: %s", log_dir)

    return logger
