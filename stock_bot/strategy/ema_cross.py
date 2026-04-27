"""EMA 크로스 전략 (지수이동평균 골든/데드 크로스).

SMA 크로스 대비 최근 가격에 더 빠르게 반응한다.
5분봉 기준 EMA(9) / EMA(21) 기본값.
  - 단기 EMA 가 장기 EMA 를 상향 돌파 → BUY
  - 단기 EMA 가 장기 EMA 를 하향 돌파 → SELL
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def decide_ema_cross(
    closes: pd.Series,
    fast: int = 9,
    slow: int = 21,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
) -> Decision:
    """closes: 오래된 → 최신 순서의 종가 Series."""
    if len(closes) < slow + 2:
        return Decision(MACrossSignal.HOLD, "not enough data")

    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    last_price = float(closes.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    prev_diff = float(ema_fast.iloc[-2] - ema_slow.iloc[-2])
    curr_diff = float(ema_fast.iloc[-1] - ema_slow.iloc[-1])

    if prev_diff <= 0 < curr_diff and position_qty == 0:
        return Decision(
            MACrossSignal.BUY,
            f"ema golden cross EMA{fast}/EMA{slow} diff={curr_diff:+.2f}",
        )
    if prev_diff >= 0 > curr_diff and position_qty > 0:
        return Decision(
            MACrossSignal.SELL,
            f"ema dead cross EMA{fast}/EMA{slow} diff={curr_diff:+.2f}",
        )
    return Decision(
        MACrossSignal.HOLD,
        f"EMA{fast}={ema_fast.iloc[-1]:.2f} EMA{slow}={ema_slow.iloc[-1]:.2f} diff={curr_diff:+.4f}",
    )
