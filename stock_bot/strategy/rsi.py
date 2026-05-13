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
    if len(closes) < 3:
        return Decision(MACrossSignal.HOLD, "not enough data")

    rsi = _rsi(closes, period)
    last_rsi = float(rsi.iloc[-1])
    last_price = float(closes.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    # 매도(과매수·손절)는 봉 부족 시에도 허용 — EWM RSI는 소수 봉에서도 유효
    if position_qty > 0 and last_rsi > overbought:
        return Decision(MACrossSignal.SELL, f"RSI {last_rsi:.1f} > {overbought}")

    # 매수는 충분한 봉 확보 후에만 허용 (오신호 방지)
    if len(closes) < period + 2:
        return Decision(MACrossSignal.HOLD, f"RSI {last_rsi:.1f} (봉부족 {len(closes)}/{period+2})")

    if last_rsi < oversold:
        return Decision(MACrossSignal.BUY, f"RSI {last_rsi:.1f} < {oversold}")

    return Decision(MACrossSignal.HOLD, f"RSI {last_rsi:.1f}")
