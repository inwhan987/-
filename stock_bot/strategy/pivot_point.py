"""Classic Pivot Point 전략.

전날 H/L/C → 당일 Pivot 레벨 계산 후 앙상블 투표자로 사용.

레벨 계산 (Standard):
  P  = (H + L + C) / 3
  R1 = 2P - L,        S1 = 2P - H
  R2 = P + (H - L),   S2 = P - (H - L)
  R3 = H + 2(P - L),  S3 = L - 2(H - P)

투표 로직 (앙상블 서브전략):
  BUY  : 현재가 > P  (피봇 위 = 당일 강세 편향)
           AND (S1 근처 반등 OR P에서 막 상향돌파)
  SELL : 현재가 < P  (피봇 아래 = 당일 약세 편향)
           AND (R1 근처 저항 OR P에서 막 하향이탈)
  HOLD : P 근방 ±proximity_pct 이내 횡보 / 데이터 부족

ohlcv_df_hist 에서 당일봉을 제외한 직전 거래일 OHLCV 를 추출해
Pivot 레벨을 계산한다. 데이터가 없으면 HOLD.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .ma_cross import Decision, MACrossSignal


@dataclass
class PivotLevels:
    P:  float
    R1: float; R2: float; R3: float
    S1: float; S2: float; S3: float


def compute_pivot(prev_high: float, prev_low: float, prev_close: float) -> PivotLevels:
    """Standard Pivot Point 레벨 계산."""
    P  = (prev_high + prev_low + prev_close) / 3.0
    R1 = 2 * P - prev_low
    S1 = 2 * P - prev_high
    R2 = P + (prev_high - prev_low)
    S2 = P - (prev_high - prev_low)
    R3 = prev_high + 2 * (P - prev_low)
    S3 = prev_low  - 2 * (prev_high - P)
    return PivotLevels(P=P, R1=R1, R2=R2, R3=R3, S1=S1, S2=S2, S3=S3)


def _get_prev_day_ohlc(ohlcv_hist: pd.DataFrame) -> tuple[float, float, float] | None:
    """ohlcv_df_hist 에서 직전 거래일의 (high, low, close) 추출.

    당일 봉을 제외하고, 가장 최근 완료 거래일의 데이터를 반환.
    반환: (high, low, close) or None (데이터 부족)
    """
    if ohlcv_hist is None or len(ohlcv_hist) < 2:
        return None
    if not {"high", "low", "close"}.issubset(ohlcv_hist.columns):
        return None

    today = ohlcv_hist.index[-1].date()
    prev = ohlcv_hist[ohlcv_hist.index.map(lambda t: t.date()) < today]
    if prev.empty:
        return None

    prev_day = prev.index[-1].date()
    day_bars = prev[prev.index.map(lambda t: t.date()) == prev_day]
    if day_bars.empty:
        return None

    return (
        float(day_bars["high"].max()),
        float(day_bars["low"].min()),
        float(day_bars["close"].iloc[-1]),
    )


def decide_pivot_ensemble(
    ohlcv_hist: pd.DataFrame,
    current_price: float | None = None,
    proximity_pct: float = 0.005,   # 레벨 근접 인정 (0.5%)
    breakout_pct: float = 0.002,    # 피봇 상/하향 돌파 인정 (0.2%)
) -> Decision | None:
    """Classic Pivot Point 기반 앙상블 투표 신호.

    BUY  : 현재가 > P + breakout_pct  (피봇 상향)
           OR S1 근처(±proximity_pct) 이면서 현재가 > P  (지지 반등)
    SELL : 현재가 < P - breakout_pct  (피봇 하향)
           OR R1 근처(±proximity_pct) 이면서 현재가 < P  (저항 막힘)
    HOLD : 그 외

    반환 None → 데이터 부족 (봉 수 미달)
    """
    prev = _get_prev_day_ohlc(ohlcv_hist)
    if prev is None:
        return None

    ph, pl, pc = prev
    if ph <= 0 or pl <= 0 or pc <= 0:
        return None

    lvl = compute_pivot(ph, pl, pc)
    price = current_price if current_price is not None else float(ohlcv_hist["close"].iloc[-1])

    near_r1 = abs(price - lvl.R1) / lvl.R1 <= proximity_pct
    near_s1 = abs(price - lvl.S1) / lvl.S1 <= proximity_pct
    near_r2 = abs(price - lvl.R2) / lvl.R2 <= proximity_pct
    near_s2 = abs(price - lvl.S2) / lvl.S2 <= proximity_pct

    above_pivot = price > lvl.P * (1 + breakout_pct)
    below_pivot = price < lvl.P * (1 - breakout_pct)

    meta = {
        "P": round(lvl.P, 2),
        "R1": round(lvl.R1, 2), "R2": round(lvl.R2, 2),
        "S1": round(lvl.S1, 2), "S2": round(lvl.S2, 2),
        "price": round(price, 2),
        "above_pivot": above_pivot,
        "near_s1": near_s1, "near_r1": near_r1,
        "prev_h": round(ph, 2), "prev_l": round(pl, 2), "prev_c": round(pc, 2),
    }

    # BUY 조건: 피봇 위에서 S1 지지 반등 OR 피봇 상향 + R1 근처 아님(저항 아래)
    if above_pivot:
        if near_s1:
            # S1에서 반등 중 (지지 확인)
            return Decision(MACrossSignal.BUY, f"pivot-S1반등(P={lvl.P:.0f},S1={lvl.S1:.0f})", meta=meta)
        if not near_r1 and not near_r2:
            # 피봇 위에 있고 저항 근처 아님 → 상승 여지
            return Decision(MACrossSignal.BUY, f"pivot-상향(P={lvl.P:.0f})", meta=meta)

    # SELL 조건: 피봇 아래 OR R1/R2 저항권
    if below_pivot:
        return Decision(MACrossSignal.SELL, f"pivot-하향(P={lvl.P:.0f})", meta=meta)
    if near_r1 or near_r2:
        return Decision(MACrossSignal.SELL, f"pivot-저항(R1={lvl.R1:.0f})", meta=meta)

    return Decision(MACrossSignal.HOLD, f"pivot-hold(P={lvl.P:.0f}±{proximity_pct*100:.1f}%)", meta=meta)
