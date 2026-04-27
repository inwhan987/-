"""백테스트용 전략 어댑터.

기존 전략 함수(closes 전용) 래핑 + 새 전략(Supertrend, StochRSI, Donchian) 구현.
모든 signal_fn: (df_slice, position_qty, avg_price, stop_loss_pct) -> "buy"|"sell"|"hold"
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_bot.strategy.bollinger import decide_bollinger
from stock_bot.strategy.ema_cross import decide_ema_cross
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.strategy.ma_cross import MACrossSignal
from stock_bot.strategy.ma_cross import decide as _decide_ma_cross
from stock_bot.strategy.macd import decide_macd
from stock_bot.strategy.momentum import decide_momentum
from stock_bot.strategy.rsi import decide_rsi


# ── 기존 전략 래퍼 ──────────────────────────────────────────────────────────

def _sig(d) -> str:
    return d.signal.value


def _closes_wrap(fn, **kw):
    """closes-only 전략을 (df_slice, pos, avg, sl) -> str 으로 변환."""
    def _inner(df: pd.DataFrame, position_qty: int, avg_price: float, stop_loss_pct: float) -> str:
        return _sig(fn(df["close"], position_qty=position_qty,
                       avg_price=avg_price, stop_loss_pct=stop_loss_pct, **kw))
    return _inner


strategy_ema_cross = _closes_wrap(decide_ema_cross, fast=9, slow=21)
strategy_ma_cross  = _closes_wrap(_decide_ma_cross, short_window=5, long_window=20)
strategy_macd      = _closes_wrap(decide_macd,      fast=5, slow=13, signal=4)
strategy_rsi       = _closes_wrap(decide_rsi,       period=14, oversold=35.0, overbought=65.0)
strategy_momentum  = _closes_wrap(decide_momentum,  period=10, threshold=0.0)
strategy_bollinger = _closes_wrap(decide_bollinger,  window=20, k=2.0)


def strategy_ensemble(df: pd.DataFrame, position_qty: int, avg_price: float, stop_loss_pct: float) -> str:
    cfg = EnsembleConfig()
    return _sig(decide_ensemble(df["close"], position_qty, avg_price, stop_loss_pct, cfg))


# ── 새 전략 ─────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 7) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def strategy_supertrend(
    df: pd.DataFrame, position_qty: int, avg_price: float, stop_loss_pct: float,
    period: int = 7, multiplier: float = 3.0,
) -> str:
    if len(df) < period + 2:
        return "hold"
    close = df["close"]
    high = df["high"]
    low = df["low"]
    mid = (high + low) / 2
    atr = _atr(df, period)

    upper_band = mid + multiplier * atr
    lower_band = mid - multiplier * atr

    # 슈퍼트렌드 라인 계산
    supertrend = pd.Series(np.nan, index=df.index)
    direction = pd.Series(1, index=df.index)  # 1=down-trend, -1=up-trend

    for i in range(period, len(df)):
        prev_st = supertrend.iloc[i - 1]
        prev_dir = direction.iloc[i - 1]

        ub = upper_band.iloc[i]
        lb = lower_band.iloc[i]
        c = close.iloc[i]
        prev_c = close.iloc[i - 1]

        # 상단 밴드 조정
        if np.isnan(prev_st):
            supertrend.iloc[i] = lb
            direction.iloc[i] = -1
            continue

        if prev_dir == 1:  # was downtrend
            if c > upper_band.iloc[i - 1]:
                supertrend.iloc[i] = lb
                direction.iloc[i] = -1
            else:
                supertrend.iloc[i] = min(ub, upper_band.iloc[i - 1]) if prev_c <= upper_band.iloc[i - 1] else ub
                direction.iloc[i] = 1
        else:  # was uptrend
            if c < lower_band.iloc[i - 1]:
                supertrend.iloc[i] = ub
                direction.iloc[i] = 1
            else:
                supertrend.iloc[i] = max(lb, lower_band.iloc[i - 1]) if prev_c >= lower_band.iloc[i - 1] else lb
                direction.iloc[i] = -1

    last_dir = int(direction.iloc[-1])
    prev_dir = int(direction.iloc[-2]) if len(direction) >= 2 else last_dir
    last_price = float(close.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return "sell"

    if prev_dir == 1 and last_dir == -1 and position_qty == 0:
        return "buy"   # 트렌드 전환 상승
    if prev_dir == -1 and last_dir == 1 and position_qty > 0:
        return "sell"  # 트렌드 전환 하락
    return "hold"


def strategy_stochrsi(
    df: pd.DataFrame, position_qty: int, avg_price: float, stop_loss_pct: float,
    rsi_period: int = 14, stoch_period: int = 14, smooth: int = 3,
    oversold: float = 0.2, overbought: float = 0.8,
) -> str:
    close = df["close"]
    if len(close) < rsi_period + stoch_period + smooth + 2:
        return "hold"

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / rsi_period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))

    # Stoch RSI
    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    stochrsi = (rsi - rsi_min) / (rsi_max - rsi_min + 1e-9)
    k = stochrsi.rolling(smooth).mean()
    d = k.rolling(smooth).mean()

    last_price = float(close.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return "sell"

    if len(k) < 2 or len(d) < 2:
        return "hold"

    prev_k, curr_k = float(k.iloc[-2]), float(k.iloc[-1])
    prev_d, curr_d = float(d.iloc[-2]), float(d.iloc[-1])

    # K가 D를 상향 돌파하며 과매도 탈출
    if prev_k < prev_d and curr_k > curr_d and curr_k < oversold + 0.15 and position_qty == 0:
        return "buy"
    # K가 D를 하향 돌파하며 과매수 진입
    if prev_k > prev_d and curr_k < curr_d and curr_k > overbought - 0.15 and position_qty > 0:
        return "sell"
    return "hold"


def strategy_donchian(
    df: pd.DataFrame, position_qty: int, avg_price: float, stop_loss_pct: float,
    period: int = 20,
) -> str:
    if len(df) < period + 2:
        return "hold"

    close = df["close"]
    high = df["high"]
    low = df["low"]
    last_price = float(close.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return "sell"

    upper = high.rolling(period).max()
    lower = low.rolling(period).min()

    # 신고가 돌파 → BUY
    if last_price >= float(upper.iloc[-2]) and position_qty == 0:
        return "buy"
    # 신저가 이탈 → SELL
    if last_price <= float(lower.iloc[-2]) and position_qty > 0:
        return "sell"
    return "hold"


def strategy_vwap_revert(
    df: pd.DataFrame, position_qty: int, avg_price: float, stop_loss_pct: float,
    band: float = 0.005,
) -> str:
    """VWAP 평균회귀: 종가가 VWAP 아래로 band% 이상 괴리 → BUY, 위로 괴리 → SELL."""
    if len(df) < 5:
        return "hold"

    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, 1)
    vwap = (tp * vol).cumsum() / vol.cumsum()

    last_price = float(df["close"].iloc[-1])
    last_vwap = float(vwap.iloc[-1])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return "sell"

    deviation = (last_price - last_vwap) / last_vwap
    if deviation < -band and position_qty == 0:
        return "buy"
    if deviation > band and position_qty > 0:
        return "sell"
    return "hold"


def strategy_swing_sr(
    df: pd.DataFrame, position_qty: int, avg_price: float, stop_loss_pct: float,
    lookback: int = 30, proximity_pct: float = 0.003, breakout: bool = True,
) -> str:
    """스윙 고/저점 지지·저항.

    lookback 봉의 로컬 고점(저항)·저점(지지)을 감지.
    - 현재가가 지지 근처(proximity_pct 이내)이고 직전봉 대비 반등 → BUY
    - 현재가가 저항 근처이고 직전봉 대비 눌림 → SELL (보유 시)
    - breakout=True: 저항 돌파 시에도 BUY
    """
    if len(df) < lookback + 4:
        return "hold"

    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values
    n = len(df)

    # 마지막 2봉 제외한 lookback 구간에서 스윙 고/저점 탐색
    window_end = n - 2
    window_start = max(0, window_end - lookback)

    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(window_start + 1, window_end - 1):
        if high[i] > high[i - 1] and high[i] > high[i + 1]:
            swing_highs.append(float(high[i]))
        if low[i] < low[i - 1] and low[i] < low[i + 1]:
            swing_lows.append(float(low[i]))

    last_price = float(close[-1])
    prev_price = float(close[-2])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return "sell"

    # 지지 반등 → BUY
    if swing_lows and position_qty == 0:
        supports_below = [s for s in swing_lows if s <= last_price]
        if supports_below:
            nearest_sup = max(supports_below)
            dist = (last_price - nearest_sup) / nearest_sup
            if dist <= proximity_pct and last_price >= prev_price:
                return "buy"

    # 저항 돌파 → BUY (breakout)
    if breakout and swing_highs and position_qty == 0:
        resistances_near = [r for r in swing_highs if prev_price <= r <= last_price * (1 + proximity_pct)]
        if resistances_near:
            return "buy"

    # 저항 눌림 → SELL (보유 시)
    if swing_highs and position_qty > 0:
        resistances_above = [r for r in swing_highs if r >= last_price]
        if resistances_above:
            nearest_res = min(resistances_above)
            dist = (nearest_res - last_price) / last_price
            if dist <= proximity_pct and last_price <= prev_price:
                return "sell"

    return "hold"


def strategy_volume_cluster(
    df: pd.DataFrame, position_qty: int, avg_price: float, stop_loss_pct: float,
    bins: int = 24, top_pct: float = 0.30, proximity_pct: float = 0.005,
) -> str:
    """볼륨 클러스터 지지·저항.

    전체 봉의 거래량을 가격 구간(bins)으로 집계 → 상위 top_pct 를 고거래량 노드(HVN)로 인식.
    - HVN 이 현재가 아래에 있고 가격이 HVN 근처에서 반등 → BUY (지지)
    - HVN 이 현재가 위에 있고 가격이 HVN 근처에서 눌림 → SELL (저항)
    """
    if len(df) < 30:
        return "hold"

    close  = df["close"].values.astype(float)
    volume = df["volume"].values.astype(float)
    high   = df["high"].values.astype(float)
    low    = df["low"].values.astype(float)

    price_min = low.min()
    price_max = high.max()
    if price_max <= price_min:
        return "hold"

    # 가격을 bins 개 구간으로 나눠 거래량 집계
    bin_edges   = np.linspace(price_min, price_max, bins + 1)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    vol_per_bin = np.zeros(bins)
    for p, v in zip(close, volume):
        idx = int((p - price_min) / (price_max - price_min) * bins)
        idx = min(idx, bins - 1)
        vol_per_bin[idx] += v

    threshold = np.percentile(vol_per_bin, (1 - top_pct) * 100)
    hvn_levels = bin_centers[vol_per_bin >= threshold]
    if len(hvn_levels) == 0:
        return "hold"

    last_price = float(close[-1])
    prev_price = float(close[-2])

    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return "sell"

    # 지지 반등: HVN 이 현재가 아래, 가격이 근처에서 올라옴
    supports = hvn_levels[hvn_levels <= last_price]
    if len(supports) > 0 and position_qty == 0:
        nearest_sup = float(supports.max())
        dist = (last_price - nearest_sup) / nearest_sup
        if dist <= proximity_pct and last_price >= prev_price:
            return "buy"

    # 저항 눌림: HVN 이 현재가 위, 가격이 근처에서 내려옴
    resistances = hvn_levels[hvn_levels > last_price]
    if len(resistances) > 0 and position_qty > 0:
        nearest_res = float(resistances.min())
        dist = (nearest_res - last_price) / last_price
        if dist <= proximity_pct and last_price <= prev_price:
            return "sell"

    return "hold"


# ── 전략 레지스트리 ──────────────────────────────────────────────────────────

STRATEGIES: dict[str, tuple[object, str]] = {
    "ensemble":   (strategy_ensemble,   "앙상블 (EMA+MACD+RSI+Momentum)"),
    "ema_cross":  (strategy_ema_cross,  "EMA Cross 9/21"),
    "macd":       (strategy_macd,       "MACD 5/13/4"),
    "rsi":        (strategy_rsi,        "RSI 14 (35/65)"),
    "momentum":   (strategy_momentum,   "Momentum ROC-10"),
    "bollinger":  (strategy_bollinger,  "Bollinger Band 20/2"),
    "ma_cross":   (strategy_ma_cross,   "MA Cross SMA 5/20"),
    "supertrend": (strategy_supertrend, "Supertrend 7/3"),
    "stochrsi":   (strategy_stochrsi,   "Stoch RSI 14/14/3"),
    "donchian":   (strategy_donchian,   "Donchian Ch. 20"),
    "vwap":         (strategy_vwap_revert,   "VWAP 평균회귀 0.5%"),
    "swing_sr":     (strategy_swing_sr,      "스윙 고/저점 S/R"),
    "vol_cluster":  (strategy_volume_cluster,"볼륨 클러스터 S/R"),
}
