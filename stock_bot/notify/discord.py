"""디스코드 웹후크 알림. URL 비어있으면 no-op.

2000자 제한 → 1900자 단위로 줄 경계에서 분할해 순차 전송.
"""
from __future__ import annotations

import httpx
from loguru import logger

from stock_bot.config import settings

_LIMIT = 1900


def _split(message: str) -> list[str]:
    """메시지를 1900자 이하 청크로 분할 (줄 단위 우선)."""
    if len(message) <= _LIMIT:
        return [message]
    chunks, buf = [], []
    for line in message.splitlines(keepends=True):
        if sum(len(l) for l in buf) + len(line) > _LIMIT:
            if buf:
                chunks.append("".join(buf))
                buf = []
        buf.append(line)
    if buf:
        chunks.append("".join(buf))
    return chunks or [message[:_LIMIT]]


def notify(message: str) -> None:
    url = settings.discord_webhook_url
    if not url:
        logger.debug("discord disabled: {}", message)
        return
    for chunk in _split(message):
        try:
            httpx.post(
                url,
                json={"content": chunk, "username": "주식알림"},
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning("discord send failed: {}", exc)
