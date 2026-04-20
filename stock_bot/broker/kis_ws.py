"""KIS WebSocket 실시간 체결가 클라이언트.

사용 예:
    async for tick in stream_ticks(["005930", "000660"]):
        print(tick.symbol, tick.price)

TR ID:
  H0STCNT0 - 국내주식 실시간 체결
  paper 환경은 ws://ops.koreainvestment.com:21000,
  real 은 ws://ops.koreainvestment.com:21000 (동일) / 실제 URL 은 문서 참고.

KIS 의 실시간 체결 응답은 파이프(`|`)로 구분된 CSV 형식이다.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Iterable

import websockets
from loguru import logger

from stock_bot.broker.kis import KISBroker
from stock_bot.config import settings

WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"
WS_URL_REAL = "ws://ops.koreainvestment.com:21000"


@dataclass
class Tick:
    symbol: str
    price: float
    volume: int
    time: str  # HHMMSS


def _ws_url() -> str:
    return WS_URL_PAPER if settings.is_paper else WS_URL_REAL


def _subscribe_payload(approval_key: str, symbol: str, tr_id: str = "H0STCNT0") -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",  # 1=등록, 2=해제
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": symbol}},
        }
    )


def _parse_tick(raw: str) -> Tick | None:
    """KIS 체결 응답 예: `0|H0STCNT0|001|005930^093000^70000^...`."""
    try:
        header, _tr_id, _count, body = raw.split("|", 3)
    except ValueError:
        return None
    if header != "0":
        return None
    parts = body.split("^")
    if len(parts) < 3:
        return None
    try:
        return Tick(symbol=parts[0], time=parts[1], price=float(parts[2]), volume=int(parts[12]) if len(parts) > 12 else 0)
    except (ValueError, IndexError):
        return None


async def stream_ticks(symbols: Iterable[str]) -> AsyncIterator[Tick]:
    """심볼 목록을 구독하고 체결 틱을 비동기 이터레이터로 반환."""
    broker = KISBroker()
    approval_key = broker.get_approval_key()
    broker.close()

    url = _ws_url()
    logger.info("ws connect {} symbols={}", url, list(symbols))
    async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
        for sym in symbols:
            await ws.send(_subscribe_payload(approval_key, sym))
            ack = await ws.recv()
            logger.debug("subscribe ack: {}", ack[:200])

        while True:
            raw = await ws.recv()
            if raw.startswith("{"):
                # PINGPONG 또는 에러 JSON
                logger.debug("ws control: {}", raw[:200])
                continue
            tick = _parse_tick(raw)
            if tick:
                yield tick


async def _demo() -> None:
    async for tick in stream_ticks(settings.symbols):
        logger.info("tick {} {} {}", tick.symbol, tick.price, tick.time)


if __name__ == "__main__":
    asyncio.run(_demo())
