# -*- coding: utf-8 -*-
"""通用 HTTP webhook 推送（预留占位）

本期不启用。设计目的：把"渠道"抽象出来后，接任意自建服务/中转只需实现一个
POST JSON 的 Pusher。约定请求体：
    {"title": str, "markdown": str, "text": str, "ts": "YYYY-MM-DD HH:MM:SS"}
可选 Bearer token（WEBHOOK_TOKEN）。真正实现留待需要时补。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from push.pushers import Pusher, PushResult

log = logging.getLogger("push.webhook")


class GenericWebhookPusher(Pusher):
    channel = "webhook"

    def __init__(self, url: str, token: str = "", timeout: float = 8.0,
                 max_retries: int = 3, retry_backoff: float = 2.0):
        self._url = url
        self._token = token
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = float(retry_backoff)
        self._client: Optional[httpx.AsyncClient] = None

    async def send_markdown(self, title: str, markdown: str) -> PushResult:
        if not self._url:
            return PushResult(False, self.channel, "未配置 WEBHOOK_URL（预留渠道）")
        payload = {
            "title": title,
            "markdown": markdown,
            "text": markdown,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(timeout=self._timeout)
            resp = await self._client.post(self._url, json=payload, headers=headers)
            ok = 200 <= resp.status_code < 300
            return PushResult(ok, self.channel,
                              "ok" if ok else f"http {resp.status_code}", resp.status_code)
        except httpx.HTTPError as e:
            return PushResult(False, self.channel, f"{type(e).__name__}: {e}")

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
