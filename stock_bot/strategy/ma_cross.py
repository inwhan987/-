"""이동평균 크로스 전략.

- 단기 MA 가 장기 MA 를 상향 돌파 -> BUY
- 단기 MA 가 장기 MA 를 하향 돌파 -> SELL
- 보유 중 평단 대비 손실률이 stop_loss_pct 초과 -> SELL (손절)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class MACrossSignal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Decision:
    signal: MACrossSignal
    reason: str


def _moving_average(closes: pd.Series, window: int) -> pd.Series:
    return closes.rolling(window=window, min_periods=window).mean()


def decide(
    closes: pd.Series,
    short_window: int,
    long_window: int,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
) -> Decision:
    """최신 캔들 기준으로 매매 시그널을 결정한다.

    closes: 오래된 -> 최신 순서의 종가 Series.
    """
    if len(closes) < long_window + 1:
        return Decision(MACrossSignal.HOLD, "not enough data")

    short_ma = _moving_average(closes, short_window)
    long_ma = _moving_average(closes, long_window)

    prev_diff = short_ma.iloc[-2] - long_ma.iloc[-2]
    curr_diff = short_ma.iloc[-1] - long_ma.iloc[-1]
    last_price = float(closes.iloc[-1])

    # 손절 우선
    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    if prev_diff <= 0 < curr_diff and position_qty == 0:
        return Decision(MACrossSignal.BUY, "golden cross")

    if prev_diff >= 0 > curr_diff and position_qty > 0:
        return Decision(MACrossSignal.SELL, "dead cross")

    return Decision(MACrossSignal.HOLD, "no cross")
