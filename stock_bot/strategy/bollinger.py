"""Bollinger Band mean-reversion 전략.

- 종가가 하단 밴드 아래 터치 후 복귀 -> BUY
- 종가가 상단 밴드 위 터치 후 복귀 -> SELL
- 손절 규칙 동일
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def _bands(closes: pd.Series, window: int, k: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = closes.rolling(window=window, min_periods=window).mean()
    std = closes.rolling(window=window, min_periods=window).std()
    upper = mid + k * std
    lower = mid - k * std
    return lower, mid, upper


def decide_bollinger(
    closes: pd.Series,
    window: int = 20,
    k: float = 2.0,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
) -> Decision:
    if len(closes) < window + 2:
        return Decision(MACrossSignal.HOLD, "not enough data")

    lower, _, upper = _bands(closes, window, k)
    prev_close = float(closes.iloc[-2])
    curr_close = float(closes.iloc[-1])
    prev_lower = float(lower.iloc[-2])
    curr_lower = float(lower.iloc[-1])
    prev_upper = float(upper.iloc[-2])
    curr_upper = float(upper.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (curr_close - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    # 하단 이탈 후 재진입
    if prev_close < prev_lower <= curr_lower and curr_close > curr_lower and position_qty == 0:
        return Decision(MACrossSignal.BUY, "BB lower rebound")
    # 상단 돌파 후 회귀
    if prev_close > prev_upper >= curr_upper and curr_close < curr_upper and position_qty > 0:
        return Decision(MACrossSignal.SELL, "BB upper revert")

    return Decision(MACrossSignal.HOLD, "no BB trigger")
