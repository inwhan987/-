"""VWAP 평균회귀 전략.

당일 누적 VWAP 기준:
  - 종가가 VWAP 아래로 band% 이상 이탈 → BUY (기관 매수단가 복귀 기대)
  - 종가가 VWAP 위로 band% 이상 이탈 → SELL

VWAP 은 당일 분봉 데이터로 계산하므로 daily 캔들 모드에서는 의미 없음.
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def decide_vwap(
    df: pd.DataFrame,
    band: float = 0.005,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
    warmup_bars: int = 12,  # 5분봉 기준 1시간 — 동시호가 물량으로 인한 초반 VWAP 왜곡 방지
) -> Decision:
    """df 컬럼: high, low, close, volume."""
    if len(df) < 5:
        return Decision(MACrossSignal.HOLD, "not enough data")

    # 초반 warmup_bars 캔들은 VWAP 계산에서 제외
    # (동시호가 집중 체결 → 첫 봉에 비정상 거래량 → cumsum VWAP 왜곡)
    if len(df) <= warmup_bars:
        return Decision(MACrossSignal.HOLD, f"VWAP warmup 중 ({len(df)}/{warmup_bars}봉)")
    df_calc = df.iloc[warmup_bars:]

    tp = (df_calc["high"] + df_calc["low"] + df_calc["close"]) / 3
    vol = df_calc["volume"].replace(0, 1)
    vwap = (tp * vol).cumsum() / vol.cumsum()

    last_price = float(df["close"].iloc[-1])
    last_vwap = float(vwap.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    dev = (last_price - last_vwap) / last_vwap if last_vwap > 0 else 0.0

    if dev < -band and position_qty == 0:
        return Decision(MACrossSignal.BUY, f"VWAP -{abs(dev)*100:.2f}% 이탈 (vwap={last_vwap:,.0f})")
    if dev > band and position_qty > 0:
        return Decision(MACrossSignal.SELL, f"VWAP +{dev*100:.2f}% 이탈 (vwap={last_vwap:,.0f})")
    return Decision(MACrossSignal.HOLD, f"VWAP dev={dev*100:+.2f}% (vwap={last_vwap:,.0f})")
