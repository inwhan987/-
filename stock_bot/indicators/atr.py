"""Average True Range (ATR).

ATR = Wilder's smoothing of True Range
True Range = max(high-low, |high-prev_close|, |low-prev_close|)

변동성 기반 포지션 사이징·손절 거리 계산에 쓴다.
"""
from __future__ import annotations

import pandas as pd


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    a = high - low
    b = (high - prev_close).abs()
    c = (low - prev_close).abs()
    return pd.concat([a, b, c], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR = RMA(TR, period). EMA alpha = 1/period."""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def atr_from_ohlcv(ohlcv: list[dict], period: int = 14) -> float:
    """리스트 OHLCV (오래된→최신 순서 권장)에서 최신 ATR 값."""
    if not ohlcv:
        return 0.0
    h = pd.Series([row["high"] for row in ohlcv])
    l = pd.Series([row["low"] for row in ohlcv])
    c = pd.Series([row["close"] for row in ohlcv])
    series = atr(h, l, c, period=period)
    last = series.iloc[-1]
    return float(last) if pd.notna(last) else 0.0
