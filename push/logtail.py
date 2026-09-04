# -*- coding: utf-8 -*-
"""读取 push 日志文件尾部若干行，供把"报错日志"附在推送消息末尾。

日志由 logsetup 的 RotatingFileHandler 写入 {log_dir}/push.log，且**落盘前已经过
RedactingFilter 脱敏**（access_token / SEC / sign / timestamp 等已打码），因此读出的
尾部可安全外发。这里再叠加一层 _scrub 兜底，双保险。
"""
from __future__ import annotations

import os
import re
from typing import List

# 兜底脱敏（与 logsetup 同口径，防止个别未经过滤器的行混入）
_SCRUB = [
    (re.compile(r"(access_token=)[0-9a-zA-Z]+"), r"\1***"),
    (re.compile(r"(SEC)[0-9a-fA-F]{16,}"), r"\1***"),
    (re.compile(r"(sign=)[^&\s]+"), r"\1***"),
    (re.compile(r"(timestamp=)\d+"), r"\1***"),
]


def _scrub_line(line: str) -> str:
    for pat, repl in _SCRUB:
        line = pat.sub(repl, line)
    return line.rstrip("\n")


def tail_log(log_dir: str, n: int = 20, name: str = "push",
             max_bytes: int = 64 * 1024) -> List[str]:
    """返回 {log_dir}/{name}.log 的最后 n 行（已脱敏）。读不到则返回空列表。

    只从文件末尾回溯 max_bytes 字节，避免读大文件；足够覆盖 n 行。
    """
    if not log_dir or n <= 0:
        return []
    path = os.path.join(log_dir, f"{name}.log")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()          # 丢弃可能被截断的半行
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return [_scrub_line(x) for x in lines[-n:] if x.strip()]
    except OSError:
        return []
