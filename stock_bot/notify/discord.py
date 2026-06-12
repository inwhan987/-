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

# 디스코드 발신자명(프로그램명)은 항상 고정. 주식프로그램 = 스톡봇 + 대장주
# 전체를 통칭하므로 봇 구분은 메시지 본문(🤖 스톡봇 / 👑 대장주봇)에서 한다.
PROGRAM_NAME = "주식알림"


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


def notify(message: str) -> None:
    """디스코드로 알림 전송. 발신자명은 PROGRAM_NAME 으로 고정.

    봇 구분은 메시지 본문 헤더(🤖 스톡봇 / 👑 대장주봇)로 표현한다.
    """
    url = settings.discord_webhook_url
    if not url:
        logger.debug("discord disabled: {}", message)
        return
    for chunk in _split(message):
        _post_with_retry(url, {"content": chunk, "username": PROGRAM_NAME})
