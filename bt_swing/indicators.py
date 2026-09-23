# -*- coding: utf-8 -*-
"""일봉 지표 계산 (종목별 벡터화).

모든 지표는 '해당 일자 종가까지의 정보'만 사용한다.
돌파 기준선처럼 당일을 빼야 하는 값은 명시적으로 .shift(1) 한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _streak(s: pd.Series) -> pd.Series:
    """양수 연속 일수. 음수/0이면 0으로 리셋."""
    pos = (s > 0).astype(int)
    grp = (pos == 0).cumsum()
    return pos.groupby(grp).cumsum()


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """종목 하나의 일봉 DataFrame에 지표 컬럼을 붙여 반환."""
    d = df.copy()
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]

    # 이동평균
    for n in (5, 10, 20, 60, 120, 200):
        d[f"ma{n}"] = c.rolling(n, min_periods=n).mean()

    # 추세
    d["ma20_slope"] = d["ma20"].pct_change(5)
    d["ma60_slope"] = d["ma60"].pct_change(10)
    d["aligned"] = ((d["ma5"] > d["ma20"]) & (d["ma20"] > d["ma60"])).astype(float)
    d["above_ma200"] = (c > d["ma200"]).astype(float)
    d["disp20"] = c / d["ma20"] - 1.0          # 20일선 이격도
    d["disp60"] = c / d["ma60"] - 1.0

    # 변동성
    d["atr"] = _atr(d, 14)
    d["atr_pct"] = d["atr"] / c
    d["bb_std"] = c.rolling(20, min_periods=20).std()
    d["bb_width"] = (d["bb_std"] * 4) / d["ma20"]
    # 최근 120일 중 밴드폭 백분위 (낮을수록 수축)
    d["bb_width_pct"] = d["bb_width"].rolling(120, min_periods=60).rank(pct=True)
    d["atr_pct_rank"] = d["atr_pct"].rolling(120, min_periods=60).rank(pct=True)

    # 위치
    d["hh20"] = h.rolling(20, min_periods=20).max().shift(1)    # 당일 제외 20일 최고가
    d["hh60"] = h.rolling(60, min_periods=60).max().shift(1)
    d["hh250"] = h.rolling(250, min_periods=120).max().shift(1)
    d["ll20"] = l.rolling(20, min_periods=20).min().shift(1)
    d["near_52w"] = c / d["hh250"]                              # 1.0이면 신고가
    d["box_pos"] = (c - d["ll20"]) / (d["hh20"] - d["ll20"]).replace(0, np.nan)

    # 모멘텀
    for n in (5, 20, 60, 120):
        d[f"ret{n}"] = c.pct_change(n)
    d["rsi2"] = _rsi(c, 2)
    d["rsi14"] = _rsi(c, 14)

    # 거래량 / 유동성
    d["vol_ma20"] = v.rolling(20, min_periods=20).mean()
    d["vol_ratio"] = v / d["vol_ma20"]
    d["value_ma20"] = d["value"].rolling(20, min_periods=20).mean()
    d["value_eok"] = d["value_ma20"] / 1e8
    if "mktcap" in d.columns:
        d["mktcap_eok"] = d["mktcap"].ffill() / 1e8
    else:
        d["mktcap_eok"] = np.nan
    # 조정 구간 거래량 감소 여부 (건전한 수축)
    d["vol_dry"] = (v.rolling(5, min_periods=5).mean()
                    / v.rolling(20, min_periods=20).mean())

    # 갭
    d["gap"] = d["open"] / c.shift(1) - 1.0

    # 캔들 품질
    rng = (h - l).replace(0, np.nan)
    d["upper_wick"] = (h - np.maximum(c, d["open"])) / rng
    d["body"] = (c - d["open"]) / rng

    # ── 수급 ──────────────────────────────────────────────────────────
    for who in ("forgn", "inst"):
        s = d[who].fillna(0.0)
        d[f"{who}_streak"] = _streak(s)
        d[f"{who}_sum5"] = s.rolling(5, min_periods=1).sum()
        d[f"{who}_sum20"] = s.rolling(20, min_periods=1).sum()
        # 순매수 강도 = 5일 누적 순매수 / 20일 평균 거래대금
        d[f"{who}_intensity"] = d[f"{who}_sum5"] / d["value_ma20"].replace(0, np.nan)
    d["both_streak"] = np.minimum(d["forgn_streak"], d["inst_streak"])
    d["flow_intensity"] = d["forgn_intensity"].fillna(0) + d["inst_intensity"].fillna(0)

    # ── 펀더멘털 (KRX 공표 PER/PBR) ────────────────────────────────────
    d["per"] = d["PER"].replace(0, np.nan).ffill()
    d["pbr"] = d["PBR"].replace(0, np.nan).ffill()
    d["eps_pos"] = (d["EPS"].ffill() > 0).astype(float)

    # 전종목(1000+)을 메모리에 올리면 float64로는 1GB를 넘는다.
    # 가격·거래량은 그대로 두고 파생 지표만 float32로 낮춘다.
    keep64 = {"open", "high", "low", "close", "volume", "value"}
    for c in d.columns:
        if c not in keep64 and d[c].dtype == np.float64:
            d[c] = d[c].astype(np.float32)

    return d


def clip01(x: pd.Series | np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(x, dtype=float), 0.0, 1.0)


def scale(x, lo: float, hi: float) -> np.ndarray:
    """lo→0, hi→1 선형 스케일 후 0~1 클립. lo>hi면 역방향."""
    x = np.asarray(x, dtype=float)
    if hi == lo:
        return np.zeros_like(x)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)
