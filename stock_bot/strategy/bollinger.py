"""Bollinger Band mean-reversion 전략.

- 종가가 하단 밴드 아래 터치 후 복귀 -> BUY  (기존)
- 하단 밴드 근처(15% 이하)에서 3봉 연속 상승 -> BUY  (추가: 반등 꺾임 감지)
- 종가가 상단 밴드 위 터치 후 회귀 (돌파 pct>=1.1) -> SELL  (기존+강화)
- 상단 밴드 근처(85% 이상)에서 3봉 연속 하락 -> SELL  (추가: 하락 꺾임 감지)
- 손절 규칙 동일

꺾임 감지 로직:
  band_pct = (종가 - 하단) / (상단 - 하단)  → 0=하단, 1.0=상단
  SELL: 3봉 전 band_pct >= 0.85 + 3봉 연속 하락 → 상단 저항 후 하락전환
  BUY:  3봉 전 band_pct <= 0.15 + 3봉 연속 상승 → 하단 지지 후 반등전환
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def _bands(closes: pd.Series, window: int, k: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = closes.rolling(window=window, min_periods=window).mean()
    std = closes.rolling(window=window, min_periods=window).std()
    upper = mid + k * std
    lower = mid - k * std
    return lower, mid, upper


def _band_pct(close: float, lower: float, upper: float) -> float:
    """밴드 내 위치 (0=하단, 1.0=상단)."""
    width = upper - lower
    if width <= 0:
        return 0.5
    return (close - lower) / width


def decide_bollinger(
    closes: pd.Series,
    window: int = 20,
    k: float = 2.0,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
    upper_near_pct: float = 0.85,   # 상단 근처 기준 (band_pct >= 이 값이면 상단권)
    lower_near_pct: float = 0.15,   # 하단 근처 기준 (band_pct <= 이 값이면 하단권)
) -> Decision:
    if len(closes) < window + 4:
        return Decision(MACrossSignal.HOLD, "not enough data")

    lower, _, upper = _bands(closes, window, k)

    c0 = float(closes.iloc[-4])   # 3봉 전
    c1 = float(closes.iloc[-3])   # 2봉 전
    c2 = float(closes.iloc[-2])   # 1봉 전
    c3 = float(closes.iloc[-1])   # 현재

    lo1 = float(lower.iloc[-3]); up1 = float(upper.iloc[-3])
    lo2 = float(lower.iloc[-2]); up2 = float(upper.iloc[-2])
    lo3 = float(lower.iloc[-1]); up3 = float(upper.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (c3 - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    if position_qty == 0:
        # ── BUY 1: 하단 이탈 후 재진입 (기존) ───────────────────────────────
        if c2 < lo2 and c3 > lo3:
            return Decision(MACrossSignal.BUY, f"BB lower rebound pct={_band_pct(c3,lo3,up3):.2f}")

        # ── BUY 2: 하단 근처에서 3봉 연속 상승 (반등 꺾임 감지) ─────────────
        bp1 = _band_pct(c1, lo1, up1)
        if bp1 <= lower_near_pct and c0 < c1 < c2 < c3:
            return Decision(
                MACrossSignal.BUY,
                f"BB lower turn (pct={bp1:.2f} bounce={c0:,.0f}→{c1:,.0f}→{c2:,.0f}→{c3:,.0f})",
            )

    if position_qty > 0:
        # ── SELL 1: 상단 돌파(pct>=1.1) 후 회귀 ─────────────────────────────
        if c2 > up2 and c3 < up3 and _band_pct(c2, lo2, up2) >= 1.1:
            return Decision(
                MACrossSignal.SELL,
                f"BB upper revert (prev={c2:,.0f} > upper={up2:,.0f})",
            )

        # ── SELL 2: 상단 근처에서 3봉 연속 하락 (꺾임 감지) ─────────────────
        bp1 = _band_pct(c1, lo1, up1)
        if bp1 >= upper_near_pct and c0 > c1 > c2 > c3:
            return Decision(
                MACrossSignal.SELL,
                f"BB upper turn (pct={bp1:.2f} peak={c0:,.0f}→{c1:,.0f}→{c2:,.0f}→{c3:,.0f})",
            )

    return Decision(MACrossSignal.HOLD, f"no BB trigger (pct={_band_pct(c3,lo3,up3):.2f})")
