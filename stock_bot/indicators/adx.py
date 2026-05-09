"""ADX (Average Directional Index).

추세 강도 지표 (0~100):
- ADX > 25: 강한 추세 (추세추종 전략 유리)
- ADX < 20: 횡보장 (역추세/평균회귀 전략 유리)
- 20~25: 애매한 구간

수식 요약:
- +DM, -DM: 방향성 움직임
- ATR(period): True Range 평균
- +DI = 100 × EMA(+DM) / ATR
- -DI = 100 × EMA(-DM) / ATR
- DX = 100 × |+DI - -DI| / (+DI + -DI)
- ADX = EMA(DX, period)
"""
from __future__ import annotations

import pandas as pd


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Wilder's ADX. period 기본 14."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    alpha = 1.0 / period
    atr_ = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_.replace(0, 1e-9)
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_.replace(0, 1e-9)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
