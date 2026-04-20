"""RSI(상대강도지수) 전략.

- RSI < oversold -> BUY (과매도 진입)
- RSI > overbought -> SELL (과매수 청산)
- 보유 중 평단 대비 stop_loss_pct 초과 손실 -> SELL
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def _rsi(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def decide_rsi(
    closes: pd.Series,
    period: int = 14,
    oversold: float = 30.0,
    overbought: float = 70.0,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
) -> Decision:
    if len(closes) < period + 2:
        return Decision(MACrossSignal.HOLD, "not enough data")

    rsi = _rsi(closes, period)
    last_rsi = float(rsi.iloc[-1])
    last_price = float(closes.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    if position_qty == 0 and last_rsi < oversold:
        return Decision(MACrossSignal.BUY, f"RSI {last_rsi:.1f} < {oversold}")

    if position_qty > 0 and last_rsi > overbought:
        return Decision(MACrossSignal.SELL, f"RSI {last_rsi:.1f} > {overbought}")

    return Decision(MACrossSignal.HOLD, f"RSI {last_rsi:.1f}")
