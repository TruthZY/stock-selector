# -*- coding: utf-8 -*-
"""钉钉群机器人推送（加签模式）

加签算法（钉钉自定义机器人官方）：
    timestamp = 当前毫秒
    sign = urlencode(base64(HMAC-SHA256(key=secret, msg=f"{timestamp}\\n{secret}")))
    最终 URL = webhook + f"&timestamp={timestamp}&sign={sign}"
timestamp 与钉钉服务器时差需 < 1 小时——服务器务必配对时区/NTP。

安全：签名后的 URL、secret 绝不写日志/返回值；PushResult.detail 只含 errcode/errmsg。
限流：钉钉机器人 20 条/分钟；本期每天每周期 1 条，远低于限，仍对"发送过快"错误退避重试。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Optional

import httpx

from push.pushers import Pusher, PushResult

log = logging.getLogger("push.dingtalk")

# 钉钉返回码：0=成功；其余见官方文档。以下视为可重试的瞬时错误
_RETRYABLE_ERRCODES = {
    130101,   # 发送太快/限流
    -1,       # 系统繁忙
    410100,   # 频率限制
}


class DingTalkPusher(Pusher):
    channel = "dingtalk"

    def __init__(self, webhook: str, secret: str = "", keyword: str = "",
                 timeout: float = 8.0, max_retries: int = 3,
                 retry_backoff: float = 2.0):
        if not webhook:
            raise ValueError("缺少钉钉 webhook（环境变量 DINGTALK_WEBHOOK）")
        # 支持两种安全模式：加签(有 secret) 或 自定义关键词(无 secret 但有 keyword)
        if not secret and not keyword:
            raise ValueError(
                "钉钉机器人需二选一：加签密钥 DINGTALK_SECRET，或自定义关键词 DINGTALK_KEYWORD")
        self._webhook = webhook
        self._secret = secret or ""
        self._keyword = keyword or ""
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = float(retry_backoff)
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def sign_mode(self) -> bool:
        """是否走加签模式（有 secret）。否则为关键词模式。"""
        return bool(self._secret)

    # -- 加签 -------------------------------------------------------------
    def _signed_url(self) -> str:
        # 关键词模式：无 secret，直接用原始 webhook（安全靠消息里带关键词）
        if not self._secret:
            return self._webhook
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{self._secret}"
        hmac_code = hmac.new(self._secret.encode("utf-8"),
                             string_to_sign.encode("utf-8"),
                             digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in self._webhook else "?"
        return f"{self._webhook}{sep}timestamp={ts}&sign={sign}"

    def _ensure_keyword(self, title: str, text: str) -> tuple[str, str]:
        """确保正文含机器人「自定义关键词」。

        钉钉对 markdown 校验的是 text 正文、对 text 类型校验的是 content，**不看
        title**；所以关键词必须落在正文里，否则即便标题带词也会被判 310000。
        """
        if not self._keyword:
            return title, text
        if self._keyword not in text:
            text = f"{text}\n\n{self._keyword}"
        return title, text

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _post(self, payload: dict) -> PushResult:
        last_detail = ""
        last_code: Optional[int] = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._get_client()
                # 每次重试都重新加签：timestamp 有时效，复用旧签名可能过期
                resp = await client.post(self._signed_url(), json=payload)
                last_code = resp.status_code
                if resp.status_code != 200:
                    last_detail = f"http {resp.status_code}"
                else:
                    data = resp.json()
                    errcode = data.get("errcode")
                    errmsg = data.get("errmsg", "")
                    if errcode == 0:
                        return PushResult(True, self.channel, "ok", resp.status_code)
                    last_detail = f"errcode={errcode} errmsg={errmsg}"
                    # 非瞬时错误（如签名/关键词不匹配）不必重试，直接返回
                    if errcode not in _RETRYABLE_ERRCODES:
                        return PushResult(False, self.channel, last_detail, resp.status_code)
            except (httpx.HTTPError, ValueError) as e:
                last_detail = f"{type(e).__name__}: {e}"
                last_code = None
            # 还有重试机会才退避
            if attempt < self._max_retries:
                delay = self._retry_backoff * (2 ** attempt)
                log.warning("钉钉推送失败(第%d次)，%.1fs 后重试：%s",
                            attempt + 1, delay, last_detail)
                await asyncio.sleep(delay)
        log.error("钉钉推送最终失败：%s", last_detail)
        return PushResult(False, self.channel, last_detail, last_code)

    # -- 对外接口 ---------------------------------------------------------
    async def send_markdown(self, title: str, markdown: str) -> PushResult:
        title, markdown = self._ensure_keyword(title, markdown)
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}
        return await self._post(payload)

    async def send_text(self, text: str) -> PushResult:
        _, text = self._ensure_keyword("", text)
        payload = {"msgtype": "text", "text": {"content": text}}
        return await self._post(payload)

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
