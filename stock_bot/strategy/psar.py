"""Parabolic SAR 추세 방향 전략.

현재가 > SAR → BUY  (SAR이 가격 아래 = 상승추세)
현재가 < SAR → SELL (SAR이 가격 위  = 하락추세)

Supertrend와 달리 가속도(AF)를 반영해 추세 강도에 따라 SAR이 빠르게 따라붙음.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .ma_cross import Decision, MACrossSignal


def _compute_psar(highs: np.ndarray, lows: np.ndarray,
                  step: float = 0.02, max_step: float = 0.2) -> np.ndarray:
    n = len(highs)
    sar = np.empty(n)
    # 첫 봉: 하락 추세로 초기화
    bull = False
    ep  = highs[0]
    af  = step
    sar[0] = lows[0]

    for i in range(1, n):
        prev_sar = sar[i - 1]
        sar_new  = prev_sar + af * (ep - prev_sar)

        if bull:
            # SAR은 직전 2봉 저가보다 낮아야
            sar_new = min(sar_new, lows[i - 1], lows[max(0, i - 2)])
            if lows[i] < sar_new:          # 추세 전환: 하락으로
                bull    = False
                sar_new = ep
                ep      = lows[i]
                af      = step
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + step, max_step)
        else:
            # SAR은 직전 2봉 고가보다 높아야
            sar_new = max(sar_new, highs[i - 1], highs[max(0, i - 2)])
            if highs[i] > sar_new:         # 추세 전환: 상승으로
                bull    = True
                sar_new = ep
                ep      = highs[i]
                af      = step
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + step, max_step)
        sar[i] = sar_new
    return sar


def decide_psar(
    ohlcv_df: pd.DataFrame,
    step: float = 0.02,
    max_step: float = 0.2,
) -> Decision | None:
    if ohlcv_df is None or len(ohlcv_df) < 3:
        return None
    highs = ohlcv_df["high"].values.astype(float)
    lows  = ohlcv_df["low"].values.astype(float)
    cur   = float(ohlcv_df["close"].iloc[-1])
    sar   = _compute_psar(highs, lows, step, max_step)
    last_sar = float(sar[-1])
    if cur > last_sar:
        return Decision(MACrossSignal.BUY,  f"psar↑ price {cur:.0f} > SAR {last_sar:.0f}")
    return Decision(MACrossSignal.SELL, f"psar↓ price {cur:.0f} < SAR {last_sar:.0f}")
