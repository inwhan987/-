"""MACD / Bollinger 전략 단위 테스트."""
from __future__ import annotations

import pandas as pd

from stock_bot.strategy import MACrossSignal
from stock_bot.strategy.bollinger import decide_bollinger
from stock_bot.strategy.macd import decide_macd


def test_macd_not_enough_data():
    assert decide_macd(pd.Series([100.0] * 10)).signal is MACrossSignal.HOLD


def test_macd_bull_cross_buys():
    # 긴 하락 추세로 MACD 가 시그널 아래에 놓인 뒤, 급반등하며 상향 돌파
    closes = pd.Series(
        [100.0 - i for i in range(40)]  # 100 -> 61
        + [62.0, 65.0]  # 이 두 캔들 사이에서 상향 돌파 발생
    )
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
