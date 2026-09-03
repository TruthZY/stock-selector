# -*- coding: utf-8 -*-
"""推送渠道抽象层

统一接口 Pusher，便于后续扩展（钉钉 / 通用 webhook / 邮件 / Telegram）。
本期只实现钉钉群机器人（加签），webhook 为占位预留。所有 send* 均为 async，
内部用已有的 httpx（异步）发请求，失败按 settings 的重试策略退避重试。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class PushResult:
    ok: bool
    channel: str
    detail: str = ""            # 成功/失败说明（已脱敏，可安全入日志）
    status_code: Optional[int] = None


class Pusher(ABC):
    """推送渠道抽象基类。"""

    channel: str = "base"

    @abstractmethod
    async def send_markdown(self, title: str, markdown: str) -> PushResult:
        """发送 markdown 消息。title 用于通知栏摘要，markdown 为正文。"""

    async def send_text(self, text: str) -> PushResult:
        """发送纯文本消息（默认包一层 markdown，子类可覆盖）。"""
        return await self.send_markdown("通知", text)

    async def aclose(self) -> None:
        """释放底层连接（可选实现）。"""
        return None


def get_pusher(settings) -> Pusher:
    """按 settings.channel 装配推送器实例。

    延迟导入具体实现，避免未配置的渠道也被 import。
    """
    channel = (getattr(settings, "channel", "dingtalk") or "dingtalk").lower()
    if channel == "dingtalk":
        from push.pushers.dingtalk import DingTalkPusher
        return DingTalkPusher(
            webhook=settings.dingtalk_webhook,
            secret=settings.dingtalk_secret,
            keyword=getattr(settings, "dingtalk_keyword", ""),
            timeout=settings.http_timeout,
            max_retries=settings.push_max_retries,
            retry_backoff=settings.push_retry_backoff,
        )
    if channel == "webhook":
        from push.pushers.webhook import GenericWebhookPusher
        return GenericWebhookPusher(
            url=settings.webhook_url,
            token=settings.webhook_token,
            timeout=settings.http_timeout,
            max_retries=settings.push_max_retries,
            retry_backoff=settings.push_retry_backoff,
        )
    raise ValueError(f"未知推送渠道 {channel!r}（支持 dingtalk / webhook）")


__all__ = ["Pusher", "PushResult", "get_pusher"]
