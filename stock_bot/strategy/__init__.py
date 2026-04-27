from __future__ import annotations

import pandas as pd

from stock_bot.config import settings

from .bollinger import decide_bollinger
from .ema_cross import decide_ema_cross
from .ensemble import EnsembleConfig, decide_ensemble
from .ma_cross import Decision, MACrossSignal, decide
from .macd import decide_macd
from .momentum import decide_momentum
from .news import decide_news
from .rsi import decide_rsi
from .supertrend import decide_supertrend
from .vwap import decide_vwap

__all__ = [
    "MACrossSignal",
    "Decision",
    "decide",
    "decide_rsi",
    "decide_macd",
    "decide_bollinger",
    "decide_ema_cross",
    "decide_momentum",
    "decide_ensemble",
    "decide_news",
    "decide_vwap",
    "decide_supertrend",
    "EnsembleConfig",
    "decide_from_settings",
]


def decide_from_settings(
    closes: pd.Series,
    position_qty: int,
    avg_price: float,
    news_sentiment: float | None = None,
    news_article_count: int = 0,
    news_critical_count: int = 0,
    ohlcv_df: pd.DataFrame | None = None,
) -> Decision:
    """settings.trade_strategy 에 따라 전략을 분기한다.

    ohlcv_df: high/low/close/volume 포함 DataFrame (오래된→최신).
              ensemble 의 VWAP·Supertrend 에 필요. 없으면 closes-only 폴백.
    """
    common = dict(
        position_qty=position_qty,
        avg_price=avg_price,
        stop_loss_pct=settings.trade_stop_loss_pct,
    )
    if settings.trade_strategy == "news":
        return decide_news(
            recent_close=float(closes.iloc[-1]),
            sentiment_score=news_sentiment or 0.0,
            article_count=news_article_count,
            buy_threshold=settings.news_buy_threshold,
            sell_threshold=settings.news_sell_threshold,
            min_articles=settings.news_min_articles,
            **common,
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
    if settings.trade_strategy == "ema_cross":
        return decide_ema_cross(
            closes,
            fast=settings.trade_ema_fast,
            slow=settings.trade_ema_slow,
            **common,
        )
    if settings.trade_strategy == "momentum":
        return decide_momentum(
            closes,
            period=settings.trade_momentum_period,
            threshold=settings.trade_momentum_threshold,
            **common,
        )
    if settings.trade_strategy == "ensemble":
        cfg = EnsembleConfig(
            weights=settings.ensemble_weights_tuple,
            buy_threshold=settings.ensemble_buy_threshold,
            sell_threshold=settings.ensemble_sell_threshold,
            min_buy_votes=settings.ensemble_min_buy_votes,
            min_sell_votes=settings.ensemble_min_sell_votes,
            vwap_band=settings.trade_vwap_band,
            supertrend_period=settings.trade_supertrend_period,
            supertrend_mult=settings.trade_supertrend_mult,
            rsi_period=settings.trade_rsi_period,
            rsi_oversold=settings.trade_rsi_oversold,
            rsi_overbought=settings.trade_rsi_overbought,
            bb_window=settings.trade_bb_window,
            bb_k=settings.trade_bb_k,
            news_weight=settings.ensemble_news_weight if settings.ensemble_use_news else 0.0,
            news_sentiment=news_sentiment,
            news_article_count=news_article_count,
            news_critical_count=news_critical_count,
            news_min_articles=settings.news_min_articles,
            news_veto_threshold=settings.ensemble_news_veto_threshold,
        )
        return decide_ensemble(closes, ohlcv_df=ohlcv_df, config=cfg, **common)
    return decide(
        closes,
        short_window=settings.trade_short_ma,
        long_window=settings.trade_long_ma,
        **common,
    )
