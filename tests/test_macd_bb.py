"""MACD / Bollinger 전략 단위 테스트."""
from __future__ import annotations

import pandas as pd

from stock_bot.strategy import MACrossSignal
from stock_bot.strategy.bollinger import decide_bollinger
from stock_bot.strategy.macd import decide_macd


def test_macd_not_enough_data():
    assert decide_macd(pd.Series([100.0] * 10)).signal is MACrossSignal.HOLD


def test_macd_bull_cross_buys():
    # 플랫 후 상승 전환 -> MACD 가 시그널을 상향 돌파
    closes = pd.Series([100.0] * 40 + [101.0, 103.0, 106.0, 110.0, 115.0])
    result = decide_macd(closes, position_qty=0)
    assert result.signal is MACrossSignal.BUY


def test_macd_stop_loss():
    closes = pd.Series([100.0] * 60)
    result = decide_macd(closes, position_qty=10, avg_price=120.0, stop_loss_pct=5.0)
    assert result.signal is MACrossSignal.SELL
    assert "stop-loss" in result.reason


def test_bollinger_not_enough_data():
    assert decide_bollinger(pd.Series([100.0] * 5)).signal is MACrossSignal.HOLD


def test_bollinger_stop_loss():
    closes = pd.Series([100.0] * 30)
    result = decide_bollinger(closes, position_qty=10, avg_price=120.0, stop_loss_pct=5.0)
    assert result.signal is MACrossSignal.SELL
