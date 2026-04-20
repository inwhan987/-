"""이동평균 크로스 전략 단위 테스트."""
from __future__ import annotations

import pandas as pd

from stock_bot.strategy import MACrossSignal, decide


def test_not_enough_data():
    closes = pd.Series([100, 101, 102])
    result = decide(closes, short_window=5, long_window=20)
    assert result.signal is MACrossSignal.HOLD


def test_golden_cross_triggers_buy():
    # 평평 -> 하락 (데드 구간 진입) -> 급등 (마지막 캔들에서 골든 크로스)
    closes = pd.Series([100.0] * 25 + [90.0, 150.0])
    result = decide(closes, short_window=3, long_window=20, position_qty=0)
    assert result.signal is MACrossSignal.BUY


def test_stop_loss_triggers_sell():
    closes = pd.Series([100] * 30)
    result = decide(
        closes,
        short_window=5,
        long_window=20,
        position_qty=10,
        avg_price=120.0,
        stop_loss_pct=5.0,
    )
    assert result.signal is MACrossSignal.SELL
    assert "stop-loss" in result.reason


def test_hold_when_no_signal():
    closes = pd.Series([100] * 30)
    result = decide(closes, short_window=5, long_window=20)
    assert result.signal is MACrossSignal.HOLD
