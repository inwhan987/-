"""Donchian Channel 추세 방향 전략.

N봉 최고가/최저가의 중간선(midline) 기준:
  현재가 > midline → BUY  (상승 구간)
  현재가 < midline → SELL (하락 구간)

단순하고 노이즈가 적어 5분봉 intraday에 안정적.
"""
from __future__ import annotations
import pandas as pd
from .ma_cross import Decision, MACrossSignal


def decide_donchian(closes: pd.Series, period: int = 20) -> Decision | None:
    if len(closes) < period + 1:
        return None
    high = float(closes.iloc[-period:].max())
    low  = float(closes.iloc[-period:].min())
    mid  = (high + low) / 2.0
    cur  = float(closes.iloc[-1])
    if cur > mid:
        return Decision(MACrossSignal.BUY,  f"donchian↑ {cur:.0f} > mid {mid:.0f} (H{high:.0f}/L{low:.0f})")
    return Decision(MACrossSignal.SELL, f"donchian↓ {cur:.0f} < mid {mid:.0f} (H{high:.0f}/L{low:.0f})")
