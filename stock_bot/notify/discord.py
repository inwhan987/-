"""디스코드 웹후크 알림. URL 비어있으면 no-op.

2000자 제한 → 1900자 단위로 줄 경계에서 분할해 순차 전송.
일시적 네트워크 이슈(SSL handshake 타임아웃 등) 대비 3회 재시도.
"""
from __future__ import annotations

import time

import httpx
from loguru import logger

from stock_bot.config import settings

_LIMIT = 1900
_MAX_RETRIES = 3
_TIMEOUT_SEC = 10.0
_RETRY_DELAY_SEC = 1.5

# 디스코드에 표시되는 발신 봇 이름. 스톡봇(앙상블)·시스템은 기본값,
# 대장주 눌림목은 별도 이름으로 보내 한 채널에서도 구분되게 한다.
BOT_STOCK = "스톡봇 🤖"
BOT_LEADER = "대장주봇 👑"


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


def _post_with_retry(url: str, payload: dict) -> bool:
    """3회 재시도 로직. 성공 시 True, 모두 실패 시 False."""
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            r = httpx.post(url, json=payload, timeout=_TIMEOUT_SEC)
            # 2xx 만 성공으로 간주
            if 200 <= r.status_code < 300:
                if attempt > 1:
                    logger.info("discord send recovered on attempt {}/{}", attempt, _MAX_RETRIES)
                return True
            last_exc = Exception(f"HTTP {r.status_code}: {r.text[:120]}")
            logger.debug("discord send non-2xx (attempt {}/{}): {}", attempt, _MAX_RETRIES, last_exc)
        except Exception as exc:
            last_exc = exc
            logger.debug("discord send error (attempt {}/{}): {}", attempt, _MAX_RETRIES, exc)
        # 마지막 시도가 아니면 잠시 대기
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_DELAY_SEC)
    logger.warning("discord send failed after {} retries: {}", _MAX_RETRIES, last_exc)
    return False


def notify(message: str, username: str = BOT_STOCK) -> None:
    """디스코드로 알림 전송. username 으로 발신 봇 이름을 구분한다.

    기본은 스톡봇(앙상블)·시스템 알림. 대장주 눌림목은 BOT_LEADER 전달.
    """
    url = settings.discord_webhook_url
    if not url:
        logger.debug("discord disabled: {}", message)
        return
    for chunk in _split(message):
        _post_with_retry(url, {"content": chunk, "username": username})
