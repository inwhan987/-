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


def _cmd_order(args: list[str]) -> None:
    """수동 주문. 주의: TRADE_DRY_RUN=true 면 실제 전송 안 됨.

    사용: python main.py order buy 005930 1
          python main.py order buy 005930 1 "상승장 전환, 외인 순매수 확인"
          python main.py order sell 005930 1 "목표가 도달"
    """
    from stock_bot.broker import KISBroker
    from stock_bot.config import settings
    from stock_bot.live.runner import _reload_env_if_changed
    from stock_bot.names import get_name
    from stock_bot.notify import notify
    from stock_bot.storage import init_db, record_trade

    if len(args) < 3:
        print("usage: python main.py order {buy|sell} <symbol> <quantity> [reason]")
        sys.exit(1)
    side, symbol, qty_str = args[0].lower(), args[1], args[2]
    manual_reason = args[3] if len(args) >= 4 else ""
    if side not in ("buy", "sell"):
        print("side must be 'buy' or 'sell'"); sys.exit(1)
    qty = int(qty_str)

    # docker exec 로 띄운 새 프로세스는 컨테이너 시작 시 박힌 env var 를 보기 때문에
    # 파일을 직접 파싱해 최신 값으로 덮어쓴다 (TRADE_DRY_RUN 등).
    _reload_env_if_changed()

    init_db()
    broker = KISBroker()
    try:
        quote = broker.get_quote(symbol)
        price = quote.price
        print(f"현재가 {symbol} = {price:,.0f}원")
        print(f"{'시뮬레이션' if settings.trade_dry_run else settings.kis_env} 모드로 {side} {qty}주 전송...")
        resp = broker.place_order(symbol, side, qty)
        print(f"응답: {resp}")
        nm = get_name(symbol)
        reason_text = f"수동 주문 | {manual_reason}" if manual_reason else "수동 주문"
        record_trade(
            symbol, side, qty, price, reason_text, str(resp),
            strategy="manual",
            details={"kind": "manual", "side": side, "price": price, "reason": manual_reason},
        )
        emoji = "🟢 **매수**" if side == "buy" else "🔴 **매도**"
        reason_line = f"\n사유: {manual_reason}" if manual_reason else ""
        notify(f"{emoji} {symbol}{f' ({nm})' if nm else ''} {qty}주 @ {price:,.0f}원\n종류: 수동 주문 (CLI){reason_line}")
    finally:
        broker.close()


COMMANDS = {
    "backtest": _cmd_backtest,
    "live": _cmd_live,
    "quote": _cmd_quote,
    "stream": _cmd_stream,
    "news": _cmd_news,
    "web": _cmd_web,
    "order": _cmd_order,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: python main.py {backtest|live|quote|stream|news|web|order} [args...]")
        sys.exit(1)
    logger.add("logs/stock_bot.log", rotation="10 MB", retention=10)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
