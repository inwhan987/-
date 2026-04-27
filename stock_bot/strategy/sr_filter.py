"""일봉 지지/저항(S/R) 레벨 계산 + 앙상블 점수 조정."""
from __future__ import annotations

import pandas as pd


def compute_daily_sr(
    daily_df: pd.DataFrame,
    lookback: int = 60,
    swing_window: int = 2,
) -> tuple[list[float], list[float]]:
    """일봉 swing high/low → (지지 레벨 리스트, 저항 레벨 리스트).

    daily_df: high/low 컬럼 포함, 오래된→최신 순.
    lookback: 최근 N봉 기준.
    swing_window: 좌우 N봉보다 높으면 swing high.
    """
    df = daily_df.tail(lookback).reset_index(drop=True)
    highs = df["high"].values.astype(float)
    lows = df["low"].values.astype(float)
    n = len(highs)
    w = swing_window

    resistances: list[float] = []
    supports: list[float] = []

    for i in range(w, n - w):
        if (all(highs[i] >= highs[i - j] for j in range(1, w + 1)) and
                all(highs[i] >= highs[i + j] for j in range(1, w + 1))):
            resistances.append(float(highs[i]))
        if (all(lows[i] <= lows[i - j] for j in range(1, w + 1)) and
                all(lows[i] <= lows[i + j] for j in range(1, w + 1))):
            supports.append(float(lows[i]))

    return _merge_levels(supports), _merge_levels(resistances)


def _merge_levels(levels: list[float], pct: float = 0.005) -> list[float]:
    """0.5% 이내 근접 레벨 → 평균으로 병합."""
    if not levels:
        return []
    sorted_lv = sorted(levels)
    merged: list[float] = [sorted_lv[0]]
    for lv in sorted_lv[1:]:
        if (lv - merged[-1]) / merged[-1] <= pct:
            merged[-1] = (merged[-1] + lv) / 2.0
        else:
            merged.append(lv)
    return merged


def sr_score_adjust(
    current_price: float,
    supports: list[float],
    resistances: list[float],
    proximity_pct: float = 0.01,
    supertrend_signal: str = "hold",
    volume_ratio: float = 1.0,
) -> tuple[float, str]:
    """S/R 기반 앙상블 점수 조정값 반환. (adjustment, tag_str)

    규칙:
      - 지지선 1% 이내     → +0.10
      - 저항선 1% 이내     → -0.15
      - 저항 돌파(0.3~2%)
          + Supertrend BUY
          + 거래량 1.5배 이상 → +0.20
    """
    adj = 0.0
    tags: list[str] = []

    # 지지선 근처
    near_support = any(abs(current_price - s) / s <= proximity_pct for s in supports)
    if near_support:
        closest_s = min(supports, key=lambda s: abs(current_price - s))
        adj += 0.10
        tags.append(f"지지근처({closest_s:,.0f})")

    # 저항선: 돌파 먼저 확인 → 그 다음 근처(미돌파) 확인
    for r in resistances:
        dist = (current_price - r) / r
        if 0.003 <= dist <= 0.02:
            # 저항 돌파 구간 (0.3% ~ 2% 위)
            if supertrend_signal == "buy" and volume_ratio >= 1.5:
                adj += 0.20
                tags.append(f"저항돌파({r:,.0f})+ST+vol{volume_ratio:.1f}x")
            break
        if -proximity_pct <= dist < 0.003:
            # 저항 아래 또는 근접 (아직 미돌파)
            adj -= 0.15
            tags.append(f"저항근처({r:,.0f})")
            break

    return adj, " ".join(tags)


def sr_voter_signal(
    ohlcv_df: pd.DataFrame,
    supports: list[float],
    resistances: list[float],
    position_qty: int = 0,
    proximity_pct: float = 0.01,
) -> str:
    """S/R 레벨 기반 독립 투표 신호 (buy/sell/hold).

    - 지지선 근처 + 가격 반등 중 → buy
    - 저항선 근처 + 가격 눌림 중 → sell
    """
    if not supports and not resistances:
        return "hold"
    if len(ohlcv_df) < 2:
        return "hold"

    last_price = float(ohlcv_df["close"].iloc[-1])
    prev_price = float(ohlcv_df["close"].iloc[-2])

    if supports and position_qty == 0:
        near_sup = [s for s in supports if abs(last_price - s) / s <= proximity_pct and s <= last_price]
        if near_sup and last_price >= prev_price:
            return "buy"

    if resistances and position_qty > 0:
        near_res = [r for r in resistances if abs(last_price - r) / r <= proximity_pct and r >= last_price]
        if near_res and last_price <= prev_price:
            return "sell"

    return "hold"
