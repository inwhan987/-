from __future__ import annotations

import pandas as pd

from stock_bot.config import settings

from .bollinger import decide_bollinger
from .ma_cross import Decision, MACrossSignal, decide
from .macd import decide_macd
from .rsi import decide_rsi

__all__ = [
    "MACrossSignal",
    "Decision",
    "decide",
    "decide_rsi",
    "decide_macd",
    "decide_bollinger",
    "decide_from_settings",
]


def decide_from_settings(
    closes: pd.Series,
    position_qty: int,
    avg_price: float,
) -> Decision:
    """settings.trade_strategy 에 따라 전략을 분기한다."""
    common = dict(
        position_qty=position_qty,
        avg_price=avg_price,
        stop_loss_pct=settings.trade_stop_loss_pct,
    )
    if settings.trade_strategy == "rsi":
        return decide_rsi(
            closes,
            period=settings.trade_rsi_period,
            oversold=settings.trade_rsi_oversold,
            overbought=settings.trade_rsi_overbought,
            **common,
        )
    if settings.trade_strategy == "macd":
        return decide_macd(
            closes,
            fast=settings.trade_macd_fast,
            slow=settings.trade_macd_slow,
            signal=settings.trade_macd_signal,
            **common,
        )
    if settings.trade_strategy == "bollinger":
        return decide_bollinger(
            closes,
            window=settings.trade_bb_window,
            k=settings.trade_bb_k,
            **common,
        )
    return decide(
        closes,
        short_window=settings.trade_short_ma,
        long_window=settings.trade_long_ma,
        **common,
    )
