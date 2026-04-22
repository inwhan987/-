"""CLI 엔트리포인트.

사용:
  python main.py backtest 005930.KS
  python main.py live
  python main.py quote 005930
  python main.py stream 005930 000660
  python main.py news 005930
  python main.py web
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


def _cmd_news(args: list[str]) -> None:
    """수동 뉴스 크롤 + 감성 분석 (스케줄러 없이 1회)."""
    from stock_bot.config import settings
    from stock_bot.news import (
        fetch_naver_news,
        init_news_db,
        recent_sentiment,
        save_news,
        score_sentiment,
    )

    init_news_db()
    symbols = args or settings.symbols
    for sym in symbols:
        items = fetch_naver_news(sym, pages=settings.news_pages_per_symbol)
        new_count = 0
        crit_count = 0
        for it in items:
            res = score_sentiment(
                f"{it.title} {it.summary}",
                prefer_llm=settings.news_prefer_llm,
                symbol=sym,
            )
            if save_news(
                it, res.score, res.method,
                weight=res.weight, is_critical=res.is_critical,
            ):
                new_count += 1
                if res.is_critical:
                    crit_count += 1
        avg, count, crit = recent_sentiment(sym, hours=settings.news_lookback_hours)
        print(
            f"{sym}: new={new_count}/total={len(items)} (critical new={crit_count}) | "
            f"recent_{settings.news_lookback_hours}h: score={avg:+.2f} ({count} articles, crit={crit})"
        )


def _cmd_web(_: list[str]) -> None:
    from stock_bot.web import run_web

    run_web()


COMMANDS = {
    "backtest": _cmd_backtest,
    "live": _cmd_live,
    "quote": _cmd_quote,
    "stream": _cmd_stream,
    "news": _cmd_news,
    "web": _cmd_web,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: python main.py {backtest|live|quote|stream|news|web} [args...]")
        sys.exit(1)
    logger.add("logs/stock_bot.log", rotation="10 MB", retention=10)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
