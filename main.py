"""CLI 엔트리포인트.

사용:
  python main.py backtest 005930.KS
  python main.py live
  python main.py quote 005930
  python main.py stream 005930 000660
"""
from __future__ import annotations

import sys

from loguru import logger


def _cmd_backtest(args: list[str]) -> None:
    from stock_bot.backtest import run_backtest

    symbol = args[0] if args else "005930.KS"
    result = run_backtest(symbol)
    for k, v in result.items():
        print(f"{k:>20}: {v}")


def _cmd_live(_: list[str]) -> None:
    from stock_bot.live import run_live

    run_live()


def _cmd_quote(args: list[str]) -> None:
    from stock_bot.broker import KISBroker

    if not args:
        print("usage: python main.py quote <symbol>")
        sys.exit(1)
    broker = KISBroker()
    try:
        print(broker.get_quote(args[0]))
    finally:
        broker.close()


def _cmd_stream(args: list[str]) -> None:
    """WebSocket 실시간 체결가 스트리밍."""
    import asyncio

    from stock_bot.broker import stream_ticks
    from stock_bot.config import settings

    symbols = args or settings.symbols

    async def runner() -> None:
        async for tick in stream_ticks(symbols):
            print(f"{tick.time} {tick.symbol} {tick.price} vol={tick.volume}")

    asyncio.run(runner())


COMMANDS = {
    "backtest": _cmd_backtest,
    "live": _cmd_live,
    "quote": _cmd_quote,
    "stream": _cmd_stream,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: python main.py {backtest|live|quote|stream} [args...]")
        sys.exit(1)
    logger.add("logs/stock_bot.log", rotation="10 MB", retention=10)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
