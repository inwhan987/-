"""Parabolic SAR 전략.

Parabolic SAR (Stop And Reverse):
  - 추세추종 지표. SAR 점이 가격 아래면 상승추세(BUY), 위면 하락추세(SELL).
  - 추세 전환 시점에 포지션 방향 전환 신호를 줌.
  - 파라미터: step(AF 초기값), max_af(AF 최대값)

앙상블 서브전략 용도:
  - 상승추세 진입: SAR < close  →  BUY
  - 하락추세 전환: SAR > close  →  SELL
  - HOLD: 충분한 데이터 없음

기본 파라미터 (5분봉 권장):
  step=0.02, max_af=0.20  — TA-Lib 기본값과 동일
  step을 키우면 SAR이 빠르게 반응 (노이즈 증가)
  step을 줄이면 느리게 반응 (추세 유지)
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def _calc_sar(
    high: "list[float] | pd.Series",
    low:  "list[float] | pd.Series",
    step: float = 0.02,
    max_af: float = 0.20,
) -> list[float]:
    """Wilder's Parabolic SAR 계산.

    반환: SAR 값 리스트 (길이 = len(high)).
    첫 번째 값은 NaN (초기화 미완)으로 0.0 처리.
    """
    hi = list(high)
    lo = list(low)
    n = len(hi)
    if n < 2:
        return [0.0] * n

    sar = [0.0] * n
    # 초기 방향: 첫 봉 종가 방향(고가>저가이면 상승 가정)
    bull = True
    af = step
    ep = hi[0]   # extreme point
    sar[0] = lo[0]

    for i in range(1, n):
        prev_sar = sar[i - 1]

        if bull:
            # 상승추세: SAR은 이전 저점보다 낮아야
            new_sar = prev_sar + af * (ep - prev_sar)
            new_sar = min(new_sar, lo[i - 1], lo[i - 2] if i >= 2 else lo[i - 1])
            if lo[i] < new_sar:
                # 반전: 하락추세 시작
                bull = False
                new_sar = ep          # 이전 고점이 새 SAR
                ep = lo[i]
                af = step
            else:
                if hi[i] > ep:
                    ep = hi[i]
                    af = min(af + step, max_af)
        else:
            # 하락추세: SAR은 이전 고점보다 높아야
            new_sar = prev_sar + af * (ep - prev_sar)
            new_sar = max(new_sar, hi[i - 1], hi[i - 2] if i >= 2 else hi[i - 1])
            if hi[i] > new_sar:
                # 반전: 상승추세 시작
                bull = True
                new_sar = ep          # 이전 저점이 새 SAR
                ep = hi[i]
                af = step
            else:
                if lo[i] < ep:
                    ep = lo[i]
                    af = min(af + step, max_af)

        sar[i] = new_sar

    return sar


def decide_parabolic_sar(
    ohlcv_df: pd.DataFrame,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
    step: float = 0.02,
    max_af: float = 0.20,
    min_bars: int = 10,
) -> Decision:
    """Parabolic SAR 기반 매매 신호.

    ohlcv_df: high/low/close 포함, 오래된→최신 순.
    """
    if ohlcv_df is None or len(ohlcv_df) < min_bars:
        return Decision(MACrossSignal.HOLD, f"parabolic-sar warmup ({len(ohlcv_df) if ohlcv_df is not None else 0}<{min_bars})")

    # 손절 (최우선)
    last_price = float(ohlcv_df["close"].iloc[-1])
    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(
                MACrossSignal.SELL,
                f"sar stop-loss {loss_pct:.2f}%",
                meta={"kind": "stop_loss", "loss_pct": loss_pct},
            )

    sar_vals = _calc_sar(
        ohlcv_df["high"].values,
        ohlcv_df["low"].values,
        step=step,
        max_af=max_af,
    )
    sar_now  = sar_vals[-1]
    sar_prev = sar_vals[-2]
    close_now  = float(ohlcv_df["close"].iloc[-1])
    close_prev = float(ohlcv_df["close"].iloc[-2])

    # 이전 봉: SAR > close (하락추세)  →  현재 봉: SAR < close (상승추세) = 상승전환
    just_turned_bull = (sar_prev > close_prev) and (sar_now < close_now)
    # 이전 봉: SAR < close (상승추세)  →  현재 봉: SAR > close (하락추세) = 하락전환
    just_turned_bear = (sar_prev < close_prev) and (sar_now > close_now)

    # 현재 추세
    bull_trend = sar_now < close_now

    meta = {
        "sar": round(sar_now, 2),
        "close": round(close_now, 2),
        "bull_trend": bull_trend,
        "just_turned_bull": just_turned_bull,
        "just_turned_bear": just_turned_bear,
        "step": step,
        "max_af": max_af,
    }

    if position_qty == 0:
        # 포지션 없음: 상승추세 진입 or 방금 상승전환 → BUY
        if bull_trend:
            return Decision(
                MACrossSignal.BUY,
                f"sar bull (SAR={sar_now:.0f} < {close_now:.0f})",
                meta=meta,
            )
    else:
        # 포지션 있음: 하락전환 → SELL
        if not bull_trend:
            return Decision(
                MACrossSignal.SELL,
                f"sar bear (SAR={sar_now:.0f} > {close_now:.0f})",
                meta=meta,
            )

    return Decision(MACrossSignal.HOLD, f"sar hold (bull={bull_trend})", meta=meta)


def decide_parabolic_sar_ensemble(
    ohlcv_df: pd.DataFrame,
    step: float = 0.02,
    max_af: float = 0.20,
    min_bars: int = 10,
) -> Decision | None:
    """앙상블 투표용 SAR 신호 (포지션/손절 로직 제외).

    반환: Decision (BUY=상승추세, SELL=하락추세, HOLD=데이터부족)
    """
    if ohlcv_df is None or len(ohlcv_df) < min_bars:
        return None

    sar_vals = _calc_sar(
        ohlcv_df["high"].values,
        ohlcv_df["low"].values,
        step=step,
        max_af=max_af,
    )
    sar_now   = sar_vals[-1]
    close_now = float(ohlcv_df["close"].iloc[-1])
    bull_trend = sar_now < close_now

    meta = {"sar": round(sar_now, 2), "close": round(close_now, 2), "bull_trend": bull_trend}

    if bull_trend:
        return Decision(MACrossSignal.BUY,  f"sar-bull({sar_now:.0f})", meta=meta)
    return Decision(MACrossSignal.SELL, f"sar-bear({sar_now:.0f})", meta=meta)
