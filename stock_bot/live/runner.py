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
from stock_bot.notify import metrics, notify
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

            decision = decide_from_settings(closes, position_qty=qty, avg_price=avg)
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
                size = max(1, settings.trade_cash_per_trade // int(price))
                resp = broker.place_order(symbol, "buy", size)
                record_trade(symbol, "buy", size, price, decision.reason, str(resp))
                metrics.orders_total.labels(symbol=symbol, side="buy", mode=mode).inc()
                notify(f"BUY {symbol} x{size} @ {price:,.0f} ({decision.reason})")

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
        id="ma_tick",
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutting down")
    finally:
        broker.close()


if __name__ == "__main__":
    run_live()
