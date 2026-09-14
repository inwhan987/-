# -*- coding: utf-8 -*-
"""야간 일봉 스캔 → 신호 → 감시 리스트 (명세 5절, 13절 4단계).

전략 조건·점수는 bt_swing/strategies.py 를 그대로 import 한다(재작성 금지).
지표도 bt_swing/indicators.compute 그대로. 이 모듈이 하는 것은
  DB 패널 로드 → compute → strat.run → 신호일 행 추출 → 진입게이트 → 순위/감시 선정
뿐이다. 같은 날짜 같은 입력이면 backtest_swing_daily.py 의 신호 수와 일치해야 한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

import numpy as np
import pandas as pd
from loguru import logger

from bt_swing import indicators, strategies

from . import axes, store
from .config import SwingCfg, cfg

RANK_BASIS = "setup_pscore"   # 감시 선정 기준. 축 점수는 기록만(SWING_TASK_AXIS_SCORE.md 2-4)

# 지표 워밍업: ma200 + hh250(shift) + atr rank 120 → 넉넉히 400봉
LOOKBACK_BARS = 400

# 트리거 유형(명세 7절). 같은 종목이 여러 전략에 걸리면 이 순서로 대표 전략을 고른다.
TRIGGER_KIND = {
    "BREAKOUT": "breakout", "NEWHIGH": "breakout", "GAPGO": "breakout", "VALUE_MOM": "breakout",
    "PULLBACK": "pullback", "FLOW_PULLBACK": "pullback", "MEANREV": "pullback",
    "FLOW_FORGN": "hold", "FLOW_INST": "hold", "FLOW_BOTH": "hold", "VALUE_PURE": "hold",
    "MOMENTUM": "hold",   # 명세 7절에 없음 — 추세 지속형이라 hold 로 둔다
}


def _entry_gate(row: pd.Series, c: SwingCfg) -> str | None:
    """bt_swing.engine._entry_gate 와 같은 판정(설정 이름만 다름). 통과=None."""
    v = row.get("value_eok", np.nan)
    if c.entry_min_value_eok > 0 and (not np.isfinite(v) or v < c.entry_min_value_eok):
        return "유동성미달"
    if c.entry_min_cap_eok > 0:
        cap = row.get("mktcap_eok", np.nan)
        if not np.isfinite(cap):
            return "시총불명"
        if cap < c.entry_min_cap_eok:
            return "시총미달"
    if c.entry_min_price > 0 and float(row.get("close", 0)) < c.entry_min_price:
        return "가격미달"
    if c.entry_max_atr_pct > 0:
        a = row.get("atr_pct", np.nan)
        if np.isfinite(a) and a > c.entry_max_atr_pct:
            return "과열"
    return None


def load_panel(date: str, codes: Iterable[str] | None = None,
               lookback: int = LOOKBACK_BARS) -> dict[str, pd.DataFrame]:
    """date 까지의 일봉 패널(원본 컬럼). 봉 수가 60 미만인 종목은 뺀다."""
    codes = list(codes) if codes is not None else store.all_codes()
    start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=int(lookback * 1.6) + 10)).strftime("%Y%m%d")
    panel = store.load_panel_raw(codes, start, date)
    return {k: v for k, v in panel.items() if len(v) >= 60}


def scan_panel(panel: dict[str, pd.DataFrame], date: str,
               strategy_names: list[str] | None, use_trend: bool) -> pd.DataFrame:
    """패널 → 신호 롱테이블 (date, code, strategy, score, 참조값들).

    backtest_swing_daily.py 와 같은 경로: compute → strat.run → signal 행.
    date 에 봉이 없는 종목(거래정지 등)은 신호 없음.
    """
    strategies.set_trend_filter(use_trend)
    strats = strategies.get(strategy_names)
    ts = pd.Timestamp(datetime.strptime(date, "%Y%m%d"))
    rows: list[dict] = []
    for code, raw in panel.items():
        if ts not in raw.index:
            continue
        d = indicators.compute(raw)
        last = d.loc[ts]
        for name, st in strats.items():
            s = st.run(d)
            if not bool(s.at[ts, "signal"]):
                continue
            rows.append({
                "date": date, "code": code, "strategy": name,
                "score": float(s.at[ts, "score"]),
                "close": float(last["close"]),
                "ma20": float(last["ma20"]), "ma60": float(last["ma60"]),
                "box_top": float(d["high"].loc[:ts].tail(20).max()),
                "atr_pct": float(last["atr_pct"]) if np.isfinite(last.get("atr_pct", np.nan)) else None,
                "vol_ma20": float(last["vol_ma20"]) if np.isfinite(last.get("vol_ma20", np.nan)) else None,
                "value_ma20": float(last["value_ma20"]) if np.isfinite(last.get("value_ma20", np.nan)) else None,
                "value_eok": float(last.get("value_eok", np.nan)),
                "mktcap_eok": float(last.get("mktcap_eok", np.nan)),
                "gate": _entry_gate(last, cfg()),
                # 축 점수 재료(기록용): per/pbr/flow5/flow20. 없으면 NaN
                **axes.materials_at(d, ts),
            })
    cols = ["date", "code", "strategy", "score", "close", "ma20", "ma60", "box_top", "atr_pct",
            "vol_ma20", "value_ma20", "value_eok", "mktcap_eok", "gate",
            "per", "pbr", "flow5", "flow20"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows)[cols].sort_values(["strategy", "score"], ascending=[True, False]).reset_index(drop=True)


def scan(date: str, strategies_: list[str] | None = None, use_trend: bool | None = None,
         codes: Iterable[str] | None = None) -> pd.DataFrame:
    """DB 에서 date 기준 스캔. 반환 = scan_panel 결과 + pscore/rank."""
    c = cfg()
    names = strategies_ if strategies_ is not None else c.strategy_names
    ut = c.use_trend_filter if use_trend is None else use_trend
    panel = load_panel(date, codes)
    logger.info("scan {}: panel {}종목, strategies={}, trend={}", date, len(panel), names or "ALL", ut)
    sig = scan_panel(panel, date, names, ut)
    sig = attach_materials(sig, date)
    return add_ranks(sig)


def attach_materials(sig: pd.DataFrame, date: str) -> pd.DataFrame:
    """DB 에서 프로그램 순매수 5일 누적(prog5)·DART 재무(roe/debt/qrev/qinc)를 신호 행에 붙인다.
    DART 는 공시 접수일 rcept_dt <= date 인 최신 행만(미래참조 방지). 없으면 NaN."""
    s = sig.copy()
    for k in ("prog5", "roe", "debt", "qrev", "qinc"):
        s[k] = np.nan
    if s.empty:
        return s
    codes = sorted(set(s["code"]))
    start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=14)).strftime("%Y%m%d")
    pg = store.load_program_range(start, date)
    if not pg.empty:
        pg = pg[pg["code"].isin(codes)].dropna(subset=["ntby_value"])
        prog5 = pg.sort_values("date").groupby("code")["ntby_value"].apply(lambda g: g.tail(5).sum())
        s["prog5"] = s["code"].map(prog5).astype(float)
    fin = store.dart_fin_asof(date)
    if fin:
        m = {"roe": "returnOnEquity", "debt": "debtToEquity", "qrev": "qtr_rev_growth", "qinc": "qtr_inc_growth"}
        for k, col in m.items():
            s[k] = s["code"].map({cd: r.get(col) for cd, r in fin.items()}).astype(float)
    return s


def add_ranks(sig: pd.DataFrame) -> pd.DataFrame:
    """전략 내 백분위(pscore)·전략 내 순위·전체 순위. 게이트 통과분만 순위 매김.

    축 점수(setup_/value_/quality_/growth_/flow_/liq_/prog_/total_score)도 여기서 붙인다 — 기록용.
    setup_score/setup_pscore 는 score/pscore 와 같은 값. 나머지 축 모집단은 게이트 통과 종목 전체.
    """
    if sig.empty:
        for col in ("pscore", "rank_in_strategy", "rank_overall", "setup_score", "setup_pscore", *axes.AXIS_COLS):
            sig[col] = pd.Series(dtype=float)
        sig["rank_basis"] = pd.Series(dtype=str)
        return sig
    s = sig.copy()
    ok = s["gate"].isna()
    s["pscore"] = np.nan
    s.loc[ok, "pscore"] = s[ok].groupby("strategy")["score"].rank(pct=True) * 100
    s["rank_in_strategy"] = np.nan
    s.loc[ok, "rank_in_strategy"] = s[ok].groupby("strategy")["score"].rank(ascending=False, method="first")
    s["rank_overall"] = np.nan
    s.loc[ok, "rank_overall"] = s.loc[ok, "pscore"].rank(ascending=False, method="first")
    # 축 점수 (기록만)
    s["setup_score"] = s["score"].astype(float)
    s["setup_pscore"] = s["pscore"]
    for col in axes.AXIS_COLS:
        s[col] = np.nan
    if ok.any():
        ax = axes.compute(s.loc[ok], key="code")
        s.loc[ok, axes.AXIS_COLS] = ax[axes.AXIS_COLS].values
    s["rank_basis"] = RANK_BASIS
    return s


def build_watchlist(signals: pd.DataFrame, mode: str, n: int) -> pd.DataFrame:
    """신규 감시 n 종목 선정 (명세 5-3).

    종목 하나에 전략 여러 개가 걸리면 pscore 가 가장 높은 전략 하나로 대표한다.
    even: 전략별로 균등 배분(전략 수로 나눔), 남는 자리는 pscore 순으로 채움.
    top : 전략 무관 pscore 상위 n.
    """
    if signals.empty:
        return signals.copy()
    s = signals[signals["gate"].isna()].copy()
    if s.empty:
        return s
    # 종목당 대표 전략
    s = s.sort_values("pscore", ascending=False).drop_duplicates("code", keep="first")
    if mode == "top" or s["strategy"].nunique() == 1:
        out = s.head(n)
    else:
        strat_list = list(s["strategy"].unique())
        per = max(1, n // len(strat_list))
        picked = s.groupby("strategy", group_keys=False).apply(lambda g: g.head(per))
        rest = s[~s.index.isin(picked.index)].head(max(0, n - len(picked)))
        out = pd.concat([picked, rest]).sort_values("pscore", ascending=False).head(n)
    out = out.copy()
    out["rank_overall"] = np.arange(1, len(out) + 1)
    return out.reset_index(drop=True)


def watchlist_rows(wl: pd.DataFrame, c: SwingCfg) -> list[dict]:
    """build_watchlist 결과 → store.save_watchlist 행. 참조값(레벨)은 levels.py 가 같이 계산."""
    from . import levels  # noqa: WPS433
    rows = []
    for _, r in wl.iterrows():
        lv = levels.from_scan_row(r, c)
        rows.append({
            "code": r["code"], "strategy": r["strategy"], "score": float(r["score"]),
            "pscore": float(r["pscore"]),
            "rank_in_strategy": int(r["rank_in_strategy"]) if pd.notna(r["rank_in_strategy"]) else None,
            "rank_overall": int(r["rank_overall"]),
            **lv, "subscribed": 0,
        })
    return rows


def run_nightly_scan(date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """스캔 → signals 기록(전부, watched 표시) → watchlist 저장. 반환 (signals, watchlist)."""
    c = cfg()
    sig = scan(date)
    wl = build_watchlist(sig, c.watch_mode, c.watch_new)
    watched = set(zip(wl["code"], wl["strategy"])) if not wl.empty else set()
    axis_cols = ["setup_score", "setup_pscore", *axes.AXIS_COLS]
    store.log_signals([{
        "date": date, "code": r["code"], "strategy": r["strategy"], "score": float(r["score"]),
        "pscore": float(r["pscore"]) if pd.notna(r["pscore"]) else None,
        "rank_overall": int(r["rank_overall"]) if pd.notna(r["rank_overall"]) else None,
        "no_trigger_reason": r["gate"] if pd.notna(r["gate"]) else None,
        "watched": 1 if (r["code"], r["strategy"]) in watched else 0,
        "rank_basis": RANK_BASIS,
        **{k: (float(r[k]) if pd.notna(r[k]) else None) for k in axis_cols},   # 비면 NULL
    } for _, r in sig.iterrows()])
    store.save_watchlist(date, watchlist_rows(wl, c))
    logger.info("scan {}: 신호 {}건(게이트통과 {}), 감시 {}종목", date, len(sig),
                int(sig["gate"].isna().sum()) if not sig.empty else 0, len(wl))
    if not sig.empty:
        logger.info("축 채움률: {}", axes.fmt_fill_rates(axes.fill_rates(sig[sig["gate"].isna()])))
    return sig, wl
