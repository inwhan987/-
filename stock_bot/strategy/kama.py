"""KAMA (Kaufman Adaptive Moving Average) 추세 방향 전략.

추세장에서는 빠르게, 횡보장에서는 느리게 움직이는 적응형 MA.
KAMA 기울기(현재 > 이전) → BUY, 반대 → SELL.

HMA와 달리 횡보 구간에서 거의 안 움직여 노이즈가 매우 적음.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .ma_cross import Decision, MACrossSignal


def _compute_kama(closes: np.ndarray, period: int, fast: int, slow: int) -> np.ndarray:
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    n = len(closes)
    kama = np.empty(n)
    kama[:period] = closes[:period]

    for i in range(period, n):
        direction = abs(closes[i] - closes[i - period])
        volatility = np.sum(np.abs(np.diff(closes[i - period:i + 1])))
        er = direction / volatility if volatility > 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (closes[i] - kama[i - 1])
    return kama


def decide_kama(
    closes: pd.Series,
    period: int = 10,
    fast: int = 2,
    slow: int = 30,
) -> Decision | None:
    min_len = period + 2
    if len(closes) < min_len:
        return None
    arr  = closes.values.astype(float)
    kama = _compute_kama(arr, period, fast, slow)
    curr = float(kama[-1])
    prev = float(kama[-2])
    slope = curr - prev
    if slope > 0:
        return Decision(MACrossSignal.BUY,  f"kama↑ {prev:.1f}→{curr:.1f} slope={slope:+.2f}")
    return Decision(MACrossSignal.SELL, f"kama↓ {prev:.1f}→{curr:.1f} slope={slope:+.2f}")
