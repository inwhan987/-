"""RSI 전략 단위 테스트."""
from __future__ import annotations

import pandas as pd

from stock_bot.strategy import MACrossSignal
from stock_bot.strategy.rsi import decide_rsi


def test_not_enough_data():
    closes = pd.Series([100, 101, 102])
    assert decide_rsi(closes).signal is MACrossSignal.HOLD


def test_oversold_triggers_buy():
    # 단조 하락 -> RSI 가 0 에 수렴해 oversold 아래
    closes = pd.Series([float(100 - i) for i in range(40)])
    result = decide_rsi(closes, period=14, oversold=30, overbought=70, position_qty=0)
    assert result.signal is MACrossSignal.BUY


def test_overbought_triggers_sell():
    closes = pd.Series([float(100 + i) for i in range(40)])
    result = decide_rsi(
        closes, period=14, oversold=30, overbought=70, position_qty=5, avg_price=100.0
    )
    assert result.signal is MACrossSignal.SELL


def test_stop_loss_in_rsi():
    closes = pd.Series([100.0] * 30)
    result = decide_rsi(
        closes, period=14, position_qty=10, avg_price=120.0, stop_loss_pct=5.0
    )
    assert result.signal is MACrossSignal.SELL
    assert "stop-loss" in result.reason
