"""디스코드 웹후크 알림. URL 비어있으면 no-op.

메시지 길이는 2000자 제한이라 1900자로 잘라서 보낸다.
"""
from __future__ import annotations

import httpx
from loguru import logger

from stock_bot.config import settings


def notify(message: str) -> None:
    url = settings.discord_webhook_url
    if not url:
        logger.debug("discord disabled: {}", message)
        return
    try:
        httpx.post(
            url,
            json={"content": message[:1900], "username": "주식알림"},
            timeout=5.0,
        )
    except Exception as exc:
        logger.warning("discord send failed: {}", exc)
