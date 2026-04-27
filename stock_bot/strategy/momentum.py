"""모멘텀(ROC) 전략 — Rate of Change 방향 전환으로 추세 포착.

N캔들 전 대비 현재 수익률(ROC)이 0선을 교차하는 순간 신호 발생.
  - ROC 가 음수 → 양수로 전환 → BUY  (하락 추세 종료, 반등 시작)
  - ROC 가 양수 → 음수로 전환 → SELL (상승 추세 종료)

5분봉 기준 period=10 (=50분 전 대비) 기본.
"""
from __future__ import annotations

import pandas as pd

from .ma_cross import Decision, MACrossSignal


def decide_momentum(
    closes: pd.Series,
    period: int = 10,
    threshold: float = 0.0,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
) -> Decision:
    """closes: 오래된 → 최신 순서의 종가 Series."""
    if len(closes) < period + 2:
        return Decision(MACrossSignal.HOLD, "not enough data")

    last_price = float(closes.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    roc = closes.pct_change(periods=period) * 100
    prev_roc = float(roc.iloc[-2])
    curr_roc = float(roc.iloc[-1])

    if prev_roc <= threshold < curr_roc and position_qty == 0:
        return Decision(
            MACrossSignal.BUY,
            f"momentum crossup ROC{period}={curr_roc:+.2f}%",
        )
    if prev_roc >= threshold > curr_roc and position_qty > 0:
        return Decision(
            MACrossSignal.SELL,
            f"momentum crossdown ROC{period}={curr_roc:+.2f}%",
        )
    return Decision(
        MACrossSignal.HOLD,
        f"ROC{period}={curr_roc:+.2f}% prev={prev_roc:+.2f}%",
    )
