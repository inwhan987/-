"""실시간 거래 러너.

장중 1분마다 실행하지는 않고, 기본 15분 주기로 일봉 데이터를 당겨 시그널을 계산한다.
KRX 정규장 (09:00 ~ 15:30 KST) 에만 동작.
"""
from __future__ import annotations

from datetime import datetime, time as dtime

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from stock_bot.broker import KISBroker
from stock_bot.config import settings
from stock_bot.indicators import atr_from_ohlcv
from stock_bot.news import (
    fetch_naver_news,
    init_news_db,
    recent_sentiment,
    save_news,
    score_sentiment,
)
from stock_bot.notify import metrics, notify
from stock_bot.sizing import SizingResult, atr_sizing, fixed_amount, fixed_fraction
from stock_bot.storage import init_db, record_trade
from stock_bot.strategy import MACrossSignal, decide_from_settings


def _is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return dtime(9, 0) <= now.time() <= dtime(15, 30)


def _positions_by_symbol(broker: KISBroker) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    for row in broker.get_positions():
        code = row.get("pdno")
        qty = int(row.get("hldg_qty", 0) or 0)
        avg = float(row.get("pchs_avg_pric", 0) or 0)
        if code and qty > 0:
            out[code] = (qty, avg)
    return out


def _get_account_value(broker: KISBroker) -> float:
    """계좌 총평가금액. 설정값이 있으면 그걸 우선, 없으면 브로커에 조회."""
    if settings.account_size_krw > 0:
        return settings.account_size_krw
    if settings.trade_dry_run:
        # dry-run 이면 브로커 조회도 실패할 수 있으니 합리적 기본값
        return max(settings.trade_cash_per_trade * 20, 10_000_000)
    return broker.get_account_total() or 10_000_000.0


def _compute_sizing(
    price: float, ohlcv: list[dict], account_value: float
) -> SizingResult:
    mode = settings.position_sizing
    if mode == "fraction":
        return fixed_fraction(account_value, settings.position_fraction, price)
    if mode == "atr":
        atr_value = atr_from_ohlcv(list(reversed(ohlcv)), period=settings.atr_period)
        return atr_sizing(
            account_value=account_value,
            risk_pct=settings.risk_per_trade_pct,
            atr_value=atr_value,
            stop_multiplier=settings.atr_stop_multiplier,
            price=price,
            max_position_pct=settings.max_position_pct,
        )
    return fixed_amount(settings.trade_cash_per_trade, price)


def _news_tick() -> None:
    """각 종목의 신규 뉴스를 가져와 감성 점수와 함께 저장."""
    if not settings.news_enabled:
        return
    for symbol in settings.symbols:
        try:
            items = fetch_naver_news(symbol, pages=settings.news_pages_per_symbol)
            new_count = 0
            for item in items:
                text = f"{item.title} {item.summary}"
                result = score_sentiment(text, prefer_llm=settings.news_prefer_llm)
                if save_news(item, result.score, result.method):
                    new_count += 1
            if new_count:
                logger.info("news {} new={} (total={})", symbol, new_count, len(items))
        except Exception as exc:
            logger.warning("news crawl failed for {}: {}", symbol, exc)


def _tick(broker: KISBroker) -> None:
    if not _is_market_open():
        logger.debug("market closed, skip")
        return

    positions = _positions_by_symbol(broker)

    lookback = max(
        settings.trade_long_ma,
        settings.trade_rsi_period,
        settings.trade_macd_slow + settings.trade_macd_signal,
        settings.trade_bb_window,
    ) + 10

    for symbol in settings.symbols:
        try:
            if settings.live_candle == "minute":
                ohlcv = broker.get_minute_ohlcv(
                    symbol, interval_min=settings.live_minute_interval, count=lookback
                )
            else:
                ohlcv = broker.get_daily_ohlcv(symbol, count=lookback)
            # KIS 는 최신이 앞이므로 역순 정렬
            closes = pd.Series([row["close"] for row in reversed(ohlcv)])
            qty, avg = positions.get(symbol, (0, 0.0))

            news_score, news_count = (0.0, 0)
            if settings.news_enabled:
                news_score, news_count = recent_sentiment(
                    symbol, hours=settings.news_lookback_hours
                )

            # ATR 모드면 손절 거리를 동적으로 계산해 전략에 주입
            effective_stop_pct = settings.trade_stop_loss_pct
            if settings.position_sizing == "atr":
                atr_val = atr_from_ohlcv(list(reversed(ohlcv)), period=settings.atr_period)
                last_price_tmp = float(closes.iloc[-1])
                if atr_val > 0 and last_price_tmp > 0:
                    dynamic_pct = (atr_val * settings.atr_stop_multiplier) / last_price_tmp * 100
                    effective_stop_pct = dynamic_pct
            # 설정을 통해 전략에 흘려보내기
            _orig_stop = settings.trade_stop_loss_pct
            settings.trade_stop_loss_pct = effective_stop_pct
            try:
                decision = decide_from_settings(
                    closes,
                    position_qty=qty,
                    avg_price=avg,
                    news_sentiment=news_score if news_count > 0 else None,
                    news_article_count=news_count,
                )
            finally:
                settings.trade_stop_loss_pct = _orig_stop
            logger.info(
                "{} [{}]: {} ({})",
                symbol,
                settings.trade_strategy,
                decision.signal.value,
                decision.reason,
            )
            metrics.last_price.labels(symbol=symbol).set(float(closes.iloc[-1]))
            metrics.position_qty.labels(symbol=symbol).set(qty)
            metrics.position_avg_price.labels(symbol=symbol).set(avg)
            mode = "dry_run" if settings.trade_dry_run else settings.kis_env

            if decision.signal is MACrossSignal.BUY:
                price = float(closes.iloc[-1])
                account_value = _get_account_value(broker)
                sizing = _compute_sizing(price, ohlcv, account_value)
                if sizing.quantity <= 0:
                    logger.warning("{}: sizing skipped ({})", symbol, sizing.note)
                    continue
                resp = broker.place_order(symbol, "buy", sizing.quantity)
                reason = f"{decision.reason} | sizing={sizing.method} {sizing.note}"
                record_trade(symbol, "buy", sizing.quantity, price, reason, str(resp))
                metrics.orders_total.labels(symbol=symbol, side="buy", mode=mode).inc()
                notify(
                    f"BUY {symbol} x{sizing.quantity} @ {price:,.0f} "
                    f"[{sizing.method}] {sizing.note} | {decision.reason}"
                )

            elif decision.signal is MACrossSignal.SELL and qty > 0:
                price = float(closes.iloc[-1])
                resp = broker.place_order(symbol, "sell", qty)
                record_trade(symbol, "sell", qty, price, decision.reason, str(resp))
                metrics.orders_total.labels(symbol=symbol, side="sell", mode=mode).inc()
                notify(f"SELL {symbol} x{qty} @ {price:,.0f} ({decision.reason})")

        except Exception as exc:
            logger.exception("tick failed for {}: {}", symbol, exc)
            metrics.tick_errors_total.labels(symbol=symbol).inc()
            notify(f"ERROR {symbol}: {exc}")


def run_live(interval_minutes: int | None = None) -> None:
    init_db()
    init_news_db()
    metrics.start_metrics_server()
    broker = KISBroker()
    interval = interval_minutes or settings.live_interval_minutes
    mode = "DRY-RUN" if settings.trade_dry_run else settings.kis_env.upper()
    notify(
        f"stock-bot started [{mode}] strategy={settings.trade_strategy} "
        f"symbols={settings.symbols}"
    )
    logger.info("live runner started, mode={} interval={}min", mode, interval)

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        _tick,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute=f"*/{interval}",
        ),
        args=[broker],
        id="trade_tick",
    )
    if settings.news_enabled:
        scheduler.add_job(
            _news_tick,
            CronTrigger(minute=f"*/{settings.news_crawl_interval_minutes}"),
            id="news_tick",
            next_run_time=datetime.now(),  # 시작 직후 1회 수행
        )
        logger.info(
            "news crawl every {} min (llm={})",
            settings.news_crawl_interval_minutes,
            settings.news_prefer_llm,
        )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutting down")
    finally:
        broker.close()


if __name__ == "__main__":
    run_live()
