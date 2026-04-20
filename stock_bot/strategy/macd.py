"""MACD (Moving Average Convergence Divergence) 전략.

- MACD(=EMA12 - EMA26) 가 시그널(EMA9) 을 **상향 돌파** -> BUY
- **하향 돌파** -> SELL
- 손절 규칙 동일
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def _macd(closes: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series]:
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd, signal_line


def decide_macd(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
) -> Decision:
    if len(closes) < slow + signal + 2:
        return Decision(MACrossSignal.HOLD, "not enough data")

    macd, sig = _macd(closes, fast, slow, signal)
    prev = float(macd.iloc[-2] - sig.iloc[-2])
    curr = float(macd.iloc[-1] - sig.iloc[-1])
    last_price = float(closes.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    if prev <= 0 < curr and position_qty == 0:
        return Decision(MACrossSignal.BUY, "MACD bull cross")
    if prev >= 0 > curr and position_qty > 0:
        return Decision(MACrossSignal.SELL, "MACD bear cross")
    return Decision(MACrossSignal.HOLD, f"MACD {curr:.3f}")
