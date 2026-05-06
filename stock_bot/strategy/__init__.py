from __future__ import annotations

import pandas as pd

from stock_bot.config import settings

from .bollinger import decide_bollinger
from .daily_context import decide_daily_context
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
    "decide_daily_context",
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
    news_strong_neg_count: int = 0,
    ohlcv_df: pd.DataFrame | None = None,
    entry_date: str | None = None,        # "YYYY-MM-DD" KST — DailyContext 용
    prev_day_high: float = 0.0,           # 전일 고가 — DailyContext 용
    prev_day_close: float = 0.0,          # 전일 종가 — DailyContext 용
    ensemble_cfg: "EnsembleConfig | None" = None,  # 틱 간 상태 유지용
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
    if settings.trade_strategy == "vwap":
        if ohlcv_df is not None:
            return decide_vwap(
                ohlcv_df, band=settings.trade_vwap_band, **common
            )
        return Decision(MACrossSignal.HOLD, "vwap requires minute candle data")
    if settings.trade_strategy == "supertrend":
        if ohlcv_df is not None:
            return decide_supertrend(
                ohlcv_df,
                period=settings.trade_supertrend_period,
                multiplier=settings.trade_supertrend_mult,
                **common,
            )
        return Decision(MACrossSignal.HOLD, "supertrend requires minute candle data")
    if settings.trade_strategy == "ensemble":
        # ensemble_cfg가 외부에서 주입되면 st_last_direction 등 틱 간 상태를 유지한 채로
        # 파라미터만 최신 settings 값으로 갱신한다.
        _prev_st_dir = ensemble_cfg.st_last_direction if ensemble_cfg is not None else None
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
            news_strong_neg_count=news_strong_neg_count,
            news_strong_neg_ratio=settings.ensemble_news_strong_neg_ratio,
            news_min_articles=settings.news_min_articles,
            news_veto_threshold=settings.ensemble_news_veto_threshold,
            # 추가매수
            add_buy_enabled=settings.add_buy_enabled,
            add_buy_threshold=settings.add_buy_threshold,
            add_buy_min_votes=settings.add_buy_min_votes,
            # DailyContext
            daily_context_entry_date=entry_date,
            daily_context_prev_day_high=prev_day_high,
            daily_context_prev_day_close=prev_day_close,
            daily_context_profit_gate_pct=settings.daily_context_profit_gate_pct,
            daily_context_avwap_pct=settings.daily_context_avwap_pct,
            daily_context_pdh_pct=settings.daily_context_pdh_pct,
            daily_context_pdc_pct=settings.daily_context_pdc_pct,
            overnight_sell_threshold=settings.overnight_sell_threshold,
            overnight_min_sell_votes=settings.overnight_min_sell_votes,
            st_last_direction=_prev_st_dir,  # 이전 틱 방향 이어받기
        )
        result = decide_ensemble(closes, ohlcv_df=ohlcv_df, config=cfg, **common)
        # 외부 cfg 객체에 업데이트된 st_last_direction 반영 (다음 틱에서 사용)
        if ensemble_cfg is not None:
            ensemble_cfg.st_last_direction = cfg.st_last_direction
        return result
    return decide(
        closes,
        short_window=settings.trade_short_ma,
        long_window=settings.trade_long_ma,
        **common,
    )
