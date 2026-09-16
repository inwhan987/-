# -*- coding: utf-8 -*-
"""축별 점수 (SWING_TASK_AXIS_SCORE.md 2절) + 종합 점수(total_score).

축 점수 자체는 기록용이지만, total_score(셋업 백분위 + 있는 축 평균)는 감시 선정 순위에 쓴다
(daily_scan.RANK_BASIS). 백테스트 기준: 셋업만으로 고르면 Q5−Q1 ≈ 0, 종합으로 고르면 +1.2%p.

같은 정의를 daily_scan(실전 기록)과 bt_swing/report(백테스트 기록) 양쪽이 쓴다.
bt_swing 을 import 하지 않는다(순수 pandas).

  재료(materials)           축        방향(높을수록 점수 ↑)
  per, pbr                  value     저PER·저PBR (양수만; 음수/0 은 재료 없음)
  roe, debt                 quality   ROE ↑, 부채비율 ↓
  qrev, qinc                growth    분기 매출·순이익 YoY ↑
  flow5, flow20             flow      외인+기관 순매수 5·20일 누적 ÷ 20일 평균 거래대금 ↑ (규모 정규화)
  value_ma20, mktcap_eok    liq       20일 평균 거래대금 ↑, 회전율(거래대금÷시총) ↑ — 시총 자체는 안 씀
  prog5                     prog      프로그램 순매수 5일 누적 ÷ 20일 평균 거래대금 ↑ (규모 정규화)

규모 정규화(2026-09-16): 수급·프로그램은 원화 합계라 시총과 같이 움직여 종합이 대형주로 쏠렸다.
거래대금으로 나눠 '그 종목 거래 규모 대비 얼마나 샀나'(strategies 의 intensity 와 같은 개념)로 바꾸고,
liq 는 시총 대신 회전율을 써서 크기 자체가 점수가 되지 않게 했다. derive() 참조.

백분위(0~100)는 그날 신호 난 종목 전체(게이트 통과분)를 모집단으로 종목 단위로 매긴다.
재료 두 개인 축은 각각 백분위 낸 뒤 평균; 하나만 있으면 있는 쪽만; 둘 다 없으면 NULL.
total_score 는 setup_pscore(있으면) + 값이 있는 축의 단순 평균. 종목 단위가 아니라 행(종목×전략) 단위 —
같은 종목이라도 전략별 셋업 백분위가 다르면 종합도 다르다. 비면 0 이 아니라 NaN(→DB NULL).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

AXES = ["value", "quality", "growth", "flow", "liq", "prog"]
AXIS_COLS = [f"{a}_score" for a in AXES] + ["total_score"]
MATERIALS = ["per", "pbr", "roe", "debt", "qrev", "qinc", "flow5", "flow20",
             "value_ma20", "mktcap_eok", "prog5"]

# (재료, 부호) — 부호 -1 은 낮을수록 좋음. *_r / turnover 는 derive() 가 재료에서 만든 파생값.
_AXIS_DEF: dict[str, list[tuple[str, int]]] = {
    "value": [("per", -1), ("pbr", -1)],
    "quality": [("roe", +1), ("debt", -1)],
    "growth": [("qrev", +1), ("qinc", +1)],
    "flow": [("flow5_r", +1), ("flow20_r", +1)],
    "liq": [("value_ma20", +1), ("turnover", +1)],
    "prog": [("prog5_r", +1)],
}
DERIVED = ["flow5_r", "flow20_r", "prog5_r", "turnover"]


def derive(df: pd.DataFrame) -> pd.DataFrame:
    """규모 정규화 파생값. value_ma20(원) 이 없거나 0 이면 NaN(→ 그 축 없음, 0 으로 안 채움).
    flow5_r/flow20_r/prog5_r = 순매수 누적(원) ÷ 20일 평균 거래대금(원)   turnover = 거래대금 ÷ 시총(억→원)"""
    out = df.copy()
    v = pd.to_numeric(out.get("value_ma20"), errors="coerce").astype(float) if "value_ma20" in out.columns         else pd.Series(np.nan, index=out.index)
    v = v.where(v > 0)
    for src, dst in (("flow5", "flow5_r"), ("flow20", "flow20_r"), ("prog5", "prog5_r")):
        out[dst] = pd.to_numeric(out[src], errors="coerce").astype(float) / v if src in out.columns else np.nan
    cap = pd.to_numeric(out.get("mktcap_eok"), errors="coerce").astype(float) * 1e8 if "mktcap_eok" in out.columns         else pd.Series(np.nan, index=out.index)
    out["turnover"] = v / cap.where(cap > 0)
    return out


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
    out = derive(out)
    have = [m for m in MATERIALS + DERIVED if m in out.columns]
    uni = out.drop_duplicates(key)[[key] + have].set_index(key)
    axes = pd.DataFrame(index=uni.index)
    for ax, parts in _AXIS_DEF.items():
        cols = []
        for m, sign in parts:
            if m in uni.columns:
                cols.append(_pct(uni[m], sign, positive_only=(ax == "value")))
        axes[f"{ax}_score"] = pd.concat(cols, axis=1).mean(axis=1, skipna=True) if cols else np.nan
    for a in AXES:
        col = f"{a}_score"
        out[col] = out[key].map(axes[col]).astype(float)
    out["total_score"] = composite(out)
    return out


def composite(df: pd.DataFrame) -> pd.Series:
    """종합 점수 = setup_pscore 와 6개 축 중 값이 있는 것의 단순 평균(행 단위). 전부 비면 NaN.
    빠진 축을 50 으로 채우지 않는다 — 자료 없는 종목이 유리해지지도 불리해지지도 않게 있는 것만 평균."""
    cols = [c for c in ["setup_pscore", *(f"{a}_score" for a in AXES)] if c in df.columns]
    if not cols:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return df[cols].apply(pd.to_numeric, errors="coerce").mean(axis=1, skipna=True).astype(float)


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
