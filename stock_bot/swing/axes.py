# -*- coding: utf-8 -*-
"""축별 점수 (SWING_TASK_AXIS_SCORE.md 2절). 기록만 — 진입/청산 판단에 쓰지 않는다.

같은 정의를 daily_scan(실전 기록)과 bt_swing/report(백테스트 기록) 양쪽이 쓴다.
bt_swing 을 import 하지 않는다(순수 pandas).

  재료(materials)           축        방향(높을수록 점수 ↑)
  per, pbr                  value     저PER·저PBR (양수만; 음수/0 은 재료 없음)
  roe, debt                 quality   ROE ↑, 부채비율 ↓
  qrev, qinc                growth    분기 매출·순이익 YoY ↑
  flow5, flow20             flow      외인+기관 순매수 5·20일 누적 ↑
  value_ma20, mktcap_eok    liq       20일 평균 거래대금 ↑, 시총 ↑
  prog5                     prog      프로그램 순매수 5일 누적 ↑

백분위(0~100)는 그날 신호 난 종목 전체(게이트 통과분)를 모집단으로 종목 단위로 매긴다.
재료 두 개인 축은 각각 백분위 낸 뒤 평균; 하나만 있으면 있는 쪽만; 둘 다 없으면 NULL.
total_score 는 있는 축의 단순 평균(참고용). 비면 0 이 아니라 NaN(→DB NULL).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

AXES = ["value", "quality", "growth", "flow", "liq", "prog"]
AXIS_COLS = [f"{a}_score" for a in AXES] + ["total_score"]
MATERIALS = ["per", "pbr", "roe", "debt", "qrev", "qinc", "flow5", "flow20",
             "value_ma20", "mktcap_eok", "prog5"]

# (재료, 부호) — 부호 -1 은 낮을수록 좋음
_AXIS_DEF: dict[str, list[tuple[str, int]]] = {
    "value": [("per", -1), ("pbr", -1)],
    "quality": [("roe", +1), ("debt", -1)],
    "growth": [("qrev", +1), ("qinc", +1)],
    "flow": [("flow5", +1), ("flow20", +1)],
    "liq": [("value_ma20", +1), ("mktcap_eok", +1)],
    "prog": [("prog5", +1)],
}


def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return np.nan
    return v if np.isfinite(v) else np.nan


def materials_at(d: pd.DataFrame, ts: pd.Timestamp) -> dict[str, float]:
    """indicators.compute 결과 프레임에서 ts 시점 재료(가격·수급·유동성). 없으면 NaN.
    flow 는 원본 forgn/inst 가 창 안에 하나라도 있을 때만 값을 낸다(fillna(0) 된 sum 컬럼 쓰지 않음)."""
    out = {k: np.nan for k in ("per", "pbr", "flow5", "flow20", "value_ma20", "mktcap_eok")}
    if ts not in d.index:
        return out
    last = d.loc[ts]
    for k in ("per", "pbr", "value_ma20", "mktcap_eok"):
        if k in d.columns:
            out[k] = _num(last.get(k))
    if "forgn" in d.columns and "inst" in d.columns:
        win = d.loc[:ts, ["forgn", "inst"]]
        for n, key in ((5, "flow5"), (20, "flow20")):
            w = win.tail(n)
            if w.notna().any().any():
                out[key] = float(w.fillna(0.0).sum().sum())
    return out


def _pct(s: pd.Series, sign: int, positive_only: bool = False) -> pd.Series:
    v = pd.to_numeric(s, errors="coerce").astype(float)
    if positive_only:
        v = v.where(v > 0)
    return (v * sign).rank(pct=True) * 100        # NaN 은 NaN 그대로


def compute(df: pd.DataFrame, key: str = "code") -> pd.DataFrame:
    """df(신호 행들, MATERIALS 일부 포함) → 축 컬럼 붙여 반환. 백분위 모집단 = key 별 유일 종목."""
    out = df.copy()
    for col in AXIS_COLS:
        out[col] = np.nan
    if out.empty:
        return out
    have = [m for m in MATERIALS if m in out.columns]
    uni = out.drop_duplicates(key)[[key] + have].set_index(key)
    axes = pd.DataFrame(index=uni.index)
    for ax, parts in _AXIS_DEF.items():
        cols = []
        for m, sign in parts:
            if m in uni.columns:
                cols.append(_pct(uni[m], sign, positive_only=(ax == "value")))
        axes[f"{ax}_score"] = pd.concat(cols, axis=1).mean(axis=1, skipna=True) if cols else np.nan
    axes["total_score"] = axes[[f"{a}_score" for a in AXES]].mean(axis=1, skipna=True)
    for col in AXIS_COLS:
        out[col] = out[key].map(axes[col]).astype(float)
    return out


def fill_rates(df: pd.DataFrame) -> dict[str, float]:
    """축별 채움률(%) — 완료 조건 (3) 출력용."""
    if df.empty:
        return {}
    r = {}
    if "setup_pscore" in df.columns:
        r["setup"] = float(df["setup_pscore"].notna().mean() * 100)
    for a in AXES:
        c = f"{a}_score"
        if c in df.columns:
            r[a] = float(df[c].notna().mean() * 100)
    return r


def fmt_fill_rates(r: dict[str, float]) -> str:
    return " / ".join(f"{k} {v:.0f}%" for k, v in r.items())
