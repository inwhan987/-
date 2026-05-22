"""Linear Regression Slope 추세 방향 전략.

N봉 선형회귀선의 기울기:
  slope > 0 → BUY  (상승 추세)
  slope < 0 → SELL (하락 추세)

HMA보다 안정적 — 전체 구간을 fitting하므로 단일 봉 노이즈에 강함.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .ma_cross import Decision, MACrossSignal


def decide_linreg(closes: pd.Series, period: int = 30) -> Decision | None:
    if len(closes) < period:
        return None
    y = closes.iloc[-period:].values.astype(float)
    x = np.arange(period, dtype=float)
    slope = float(np.polyfit(x, y, 1)[0])
    price = float(closes.iloc[-1])
    norm  = slope / price * 100  # 가격 대비 % 기울기
    if slope > 0:
        return Decision(MACrossSignal.BUY,  f"linreg↑ slope={norm:+.3f}%/봉")
    return Decision(MACrossSignal.SELL, f"linreg↓ slope={norm:+.3f}%/봉")
