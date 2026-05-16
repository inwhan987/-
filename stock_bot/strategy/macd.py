"""MACD (Moving Average Convergence Divergence) 전략.

두 가지 모드:
  decide_macd          — 크로스오버 기반 (독립 전략용)
  decide_macd_ensemble — 히스토그램 방향 기반 (앙상블 6번째 전략용)
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def _macd(closes: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series]:
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd, signal_line


def decide_macd(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
) -> Decision:
    """크로스오버 기반 (독립 전략용). 진입 순간에만 BUY/SELL."""
    if len(closes) < slow + signal + 2:
        return Decision(MACrossSignal.HOLD, "not enough data")

    macd, sig = _macd(closes, fast, slow, signal)
    prev = float(macd.iloc[-2] - sig.iloc[-2])
    curr = float(macd.iloc[-1] - sig.iloc[-1])
    last_price = float(closes.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    if prev <= 0 < curr and position_qty == 0:
        return Decision(MACrossSignal.BUY, "MACD bull cross")
    if prev >= 0 > curr and position_qty > 0:
        return Decision(MACrossSignal.SELL, "MACD bear cross")
    return Decision(MACrossSignal.HOLD, f"MACD {curr:.3f}")


def decide_macd_ensemble(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Decision:
    """히스토그램 방향 기반 (앙상블 6번째 전략용).

    크로스오버 방식은 진입 1봉에만 신호가 나와 앙상블 기여가 거의 없음.
    히스토그램(MACD - signal) 방향으로 지속 신호를 제공:
      - 히스토그램 > 0 AND 증가 → BUY  (상승 모멘텀 강화 중)
      - 히스토그램 < 0 AND 감소 → SELL (하락 모멘텀 강화 중)
      - 그 외               → HOLD
    """
    if len(closes) < slow + signal + 2:
        return Decision(MACrossSignal.HOLD, "macd-warmup")

    macd_line, sig_line = _macd(closes, fast, slow, signal)
    hist = macd_line - sig_line
    curr_h = float(hist.iloc[-1])
    prev_h = float(hist.iloc[-2])

    if curr_h > 0 and curr_h > prev_h:
        return Decision(MACrossSignal.BUY, f"MACD hist+{curr_h:.4f}(up)")
    if curr_h < 0 and curr_h < prev_h:
        return Decision(MACrossSignal.SELL, f"MACD hist{curr_h:.4f}(down)")
    return Decision(MACrossSignal.HOLD, f"MACD hist={curr_h:.4f}(flat)")
