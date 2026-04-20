"""텔레그램 알림. 토큰이 비어있으면 no-op."""
from __future__ import annotations

import httpx
from loguru import logger

from stock_bot.config import settings


def notify(message: str) -> None:
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        logger.debug("telegram disabled: {}", message)
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5.0,
        )
    except Exception as exc:
        logger.warning("telegram send failed: {}", exc)
