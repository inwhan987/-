"""DailyContext: 1일 이상 보유 포지션 청산 전략 (앙상블 5번째 투표자).

나머지 4개 전략(VWAP·Supertrend·RSI·Bollinger)은 당일 장중 신호만 보기 때문에
전날 이전에 매수한 포지션에 대한 청산 판단을 제대로 하지 못한다.
DailyContext는 이 공백을 채워 보유일수 >= 1일인 포지션에 한해 차익실현을 판단한다.

BUY 신호 없음 — SELL / HOLD 전용.

판단 흐름:
  [Gate 1] 보유일수 >= 1일  (당일 진입 포지션 제외)
  [Gate 2] 평단 대비 수익   >= profit_gate_pct  (기본 1.5%)
  → 두 게이트 통과 후 플로팅 조건 1개 이상 충족 시 SELL

플로팅 조건 (하나 이상):
  1. 세션 VWAP 대비 현재가  >= avwap_pct  (기본 +1.5%)
  2. 전일 고가  대비 현재가  >= pdh_pct   (기본 +1.0%)
  3. 전일 종가  대비 현재가  >= pdc_pct   (기본 +1.5%)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from .ma_cross import Decision, MACrossSignal

_KST = timezone(timedelta(hours=9))


def _session_vwap(ohlcv_df: pd.DataFrame) -> float:
    """분봉 DataFrame → 세션 VWAP (typical price × volume 가중평균)."""
    try:
        typical = (ohlcv_df["high"] + ohlcv_df["low"] + ohlcv_df["close"]) / 3
        vol = ohlcv_df["volume"].replace(0, 1)
        total_vol = float(vol.sum())
        if total_vol <= 0:
            return float(ohlcv_df["close"].iloc[-1])
        return float((typical * vol).sum() / total_vol)
    except Exception:
        return float(ohlcv_df["close"].iloc[-1]) if not ohlcv_df.empty else 0.0


def decide_daily_context(
    ohlcv_df: pd.DataFrame | None,
    position_qty: int,
    avg_price: float,
    entry_date: str | None = None,      # "YYYY-MM-DD" KST
    prev_day_high: float = 0.0,
    prev_day_close: float = 0.0,
    profit_gate_pct: float = 1.5,
    avwap_pct: float = 1.5,
    pdh_pct: float = 1.0,
    pdc_pct: float = 1.5,
    supertrend_bullish: bool | None = None,  # True=상승추세, None/False=기본값 유지
    trend_bonus: float = 0.5,   # 상승추세 시 임계값 가산 %p
) -> Decision:
    """1일 이상 보유 포지션의 차익실현 청산 판단.

    당일 장중 신호만 보는 나머지 전략들이 커버하지 못하는
    전날 이전 매수 포지션에 대해 청산 여부를 결정한다.

    Returns
    -------
    Decision with signal SELL or HOLD (BUY 없음).
    """
    # ── Supertrend 상승추세 시 임계값 상향 (추세 중 조기익절 방지) ────
    if supertrend_bullish is True:
        profit_gate_pct += trend_bonus
        pdc_pct += trend_bonus
        avwap_pct += trend_bonus
        pdh_pct += trend_bonus

    # ── 포지션 없으면 즉시 HOLD ────────────────────────────────────────
    if position_qty <= 0 or avg_price <= 0:
        return Decision(MACrossSignal.HOLD, "daily_context: 포지션 없음")

    if ohlcv_df is None or ohlcv_df.empty:
        return Decision(MACrossSignal.HOLD, "daily_context: ohlcv 없음")

    last_price = float(ohlcv_df["close"].iloc[-1])
    today_str = datetime.now(tz=_KST).strftime("%Y-%m-%d")

    # ── Gate 1: 보유일수 >= 1일 ───────────────────────────────────────
    # entry_date=None → DB 기록 없는 포지션(수동매수·재시작 등) → 당일진입 아닌 것으로 처리
    if entry_date is not None and entry_date >= today_str:
        return Decision(
            MACrossSignal.HOLD,
            f"daily_context: gate1 실패 (진입={entry_date}, 오늘={today_str})",
        )

    # ── Gate 2: 수익 >= profit_gate_pct ──────────────────────────────
    profit_pct = (last_price - avg_price) / avg_price * 100
    if profit_pct < profit_gate_pct:
        return Decision(
            MACrossSignal.HOLD,
            f"daily_context: gate2 실패 수익={profit_pct:.2f}% < {profit_gate_pct}%",
        )

    # ── Floating conditions ───────────────────────────────────────────
    hits: list[str] = []

    # 1. 세션 VWAP 대비 +avwap_pct%
    vwap = _session_vwap(ohlcv_df)
    if vwap > 0 and last_price >= vwap * (1 + avwap_pct / 100):
        hits.append(f"AVWAP+{avwap_pct}%(현재{last_price:.0f}≥VWAP{vwap:.0f})")

    # 2. 전일 고가 대비 +pdh_pct%
    if prev_day_high > 0 and last_price >= prev_day_high * (1 + pdh_pct / 100):
        hits.append(f"전일고가+{pdh_pct}%(현재{last_price:.0f}≥고가{prev_day_high:.0f})")

    # 3. 전일 종가 대비 +pdc_pct%
    if prev_day_close > 0 and last_price >= prev_day_close * (1 + pdc_pct / 100):
        hits.append(f"전일종가+{pdc_pct}%(현재{last_price:.0f}≥종가{prev_day_close:.0f})")

    if hits:
        return Decision(
            MACrossSignal.SELL,
            f"장기보유 청산: 수익{profit_pct:.2f}% [{' | '.join(hits)}]",
        )

    cands = []
    if vwap > 0:
        cands.append(f"VWAP{vwap:.0f}({last_price/vwap*100-100:+.2f}%)")
    if prev_day_high > 0:
        cands.append(f"전일고{prev_day_high:.0f}({last_price/prev_day_high*100-100:+.2f}%)")
    if prev_day_close > 0:
        cands.append(f"전일종{prev_day_close:.0f}({last_price/prev_day_close*100-100:+.2f}%)")
    return Decision(
        MACrossSignal.HOLD,
        f"daily_context: 게이트 통과(수익{profit_pct:.2f}%) 플로팅 미달 [{' '.join(cands)}]",
    )
