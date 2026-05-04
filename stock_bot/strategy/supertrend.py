"""Supertrend 추세추종 전략.

ATR 기반 밴드로 추세 방향을 판단:
  - 상승추세 전환 (하락→상승) → BUY
  - 하락추세 전환 (상승→하락) → SELL
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .ma_cross import Decision, MACrossSignal


def _supertrend(
    df: pd.DataFrame, period: int, multiplier: float
) -> tuple[np.ndarray, np.ndarray]:
    """(supertrend_values, direction) 반환. direction: 1=상승, -1=하락."""
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(df)

    # ATR (EWM)
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - np.roll(close, 1)),
        np.abs(low - np.roll(close, 1)),
    ])
    tr[0] = high[0] - low[0]
    alpha = 1.0 / period
    atr = np.empty(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]

    mid = (high + low) / 2.0
    ub_basic = mid + multiplier * atr
    lb_basic = mid - multiplier * atr

    ub = ub_basic.copy()
    lb = lb_basic.copy()
    st = np.empty(n)
    direction = np.ones(n, dtype=np.int8)  # 1=상승, -1=하락

    # 첫 봉은 상승추세 가정
    st[0] = lb[0]
    direction[0] = 1

    for i in range(1, n):
        # 상단 밴드: 직전 종가가 상단 이하면 좁히기
        ub[i] = min(ub_basic[i], ub[i - 1]) if close[i - 1] <= ub[i - 1] else ub_basic[i]
        # 하단 밴드: 직전 종가가 하단 이상이면 올리기
        lb[i] = max(lb_basic[i], lb[i - 1]) if close[i - 1] >= lb[i - 1] else lb_basic[i]

        prev_dir = direction[i - 1]
        if prev_dir == 1:  # 상승추세
            if close[i] < lb[i]:
                direction[i] = -1  # 하락 전환
                st[i] = ub[i]
            else:
                direction[i] = 1
                st[i] = lb[i]
        else:  # 하락추세
            if close[i] > ub[i]:
                direction[i] = 1  # 상승 전환
                st[i] = lb[i]
            else:
                direction[i] = -1
                st[i] = ub[i]

    return st, direction


def decide_supertrend(
    df: pd.DataFrame,
    period: int = 7,
    multiplier: float = 3.0,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
    prev_known_direction: int | None = None,  # 이전 틱에서 기록한 방향 (-1/1), None이면 기존 방식
) -> Decision:
    """df 컬럼: high, low, close."""
    if len(df) < period + 2:
        return Decision(MACrossSignal.HOLD, "not enough data")

    _, direction = _supertrend(df, period, multiplier)
    last_price = float(df["close"].iloc[-1])
    curr_dir = int(direction[-1])
    # 이전 틱 방향이 있으면 그걸로 전환 판단 (캔들 완성 시 방향 재계산으로 인한 누락 방지)
    prev_dir = prev_known_direction if prev_known_direction is not None else int(direction[-2])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    if prev_dir == -1 and curr_dir == 1 and position_qty == 0:
        return Decision(MACrossSignal.BUY, f"Supertrend 상승 전환 (p={period}, m={multiplier})")
    if prev_dir == 1 and curr_dir == -1 and position_qty > 0:
        return Decision(MACrossSignal.SELL, f"Supertrend 하락 전환 (p={period}, m={multiplier})")
    trend_str = "상승" if curr_dir == 1 else "하락"
    return Decision(MACrossSignal.HOLD, f"Supertrend {trend_str}추세 유지")
