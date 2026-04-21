"""ATR (Average True Range) 테스트."""
from __future__ import annotations

import pandas as pd

from stock_bot.indicators import atr, atr_from_ohlcv, true_range


def test_true_range_uses_max_of_three_ranges():
    high = pd.Series([110.0, 112.0, 115.0])
    low = pd.Series([100.0, 108.0, 111.0])
    close = pd.Series([105.0, 110.0, 113.0])
    tr = true_range(high, low, close)
    # 첫 TR 은 prev_close 가 NaN -> high-low = 10
    assert tr.iloc[0] == 10.0
    # 2번째: max(112-108, |112-105|, |108-105|) = max(4,7,3) = 7
    assert tr.iloc[1] == 7.0
    # 3번째: max(115-111, |115-110|, |111-110|) = max(4,5,1) = 5
    assert tr.iloc[2] == 5.0


def test_atr_produces_finite_value_with_enough_bars():
    rows = [
        {"high": 100 + i, "low": 95 + i, "close": 97 + i} for i in range(30)
    ]
    h = pd.Series([r["high"] for r in rows])
    l = pd.Series([r["low"] for r in rows])
    c = pd.Series([r["close"] for r in rows])
    series = atr(h, l, c, period=14)
    assert pd.notna(series.iloc[-1])
    assert series.iloc[-1] > 0


def test_atr_from_ohlcv_handles_empty():
    assert atr_from_ohlcv([], period=14) == 0.0


def test_atr_from_ohlcv_returns_last_value():
    rows = [
        {"open": 100, "high": 105, "low": 95, "close": 100 + i * 0.1}
        for i in range(30)
    ]
    val = atr_from_ohlcv(rows, period=14)
    assert val > 0
