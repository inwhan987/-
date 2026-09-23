# -*- coding: utf-8 -*-
"""성과 지표 + IC(정보계수) 측정 + 리포트 출력."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 246


# ── 성과 지표 ─────────────────────────────────────────────────────────
def perf(equity: pd.Series, trades: pd.DataFrame) -> dict:
    if equity is None or len(equity) < 2:
        return {}
    eq = equity.astype(float)
    ret = eq.pct_change().fillna(0.0)
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / eq.iloc[0] - 1.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1.0
    dd = eq / eq.cummax() - 1.0
    mdd = float(dd.min())
    vol = float(ret.std() * np.sqrt(TRADING_DAYS))
    sharpe = float(cagr / vol) if vol > 1e-9 else 0.0
    downside = ret[ret < 0].std() * np.sqrt(TRADING_DAYS)
    sortino = float(cagr / downside) if downside and downside > 1e-9 else 0.0

    out = {
        "기간(년)": round(years, 2),
        "총수익률": total,
        "CAGR": cagr,
        "MDD": mdd,
        "변동성": vol,
        "샤프": sharpe,
        "소르티노": sortino,
        "칼마": float(cagr / abs(mdd)) if mdd < -1e-9 else 0.0,
        "거래수": 0,
    }
    if trades is not None and len(trades):
        w = trades[trades["ret_net"] > 0]
        l = trades[trades["ret_net"] <= 0]
        avg_w = float(w["ret_net"].mean()) if len(w) else 0.0
        avg_l = float(l["ret_net"].mean()) if len(l) else 0.0
        out.update({
            "거래수": int(len(trades)),
            "승률": float(len(w) / len(trades)),
            "평균수익": float(trades["ret_net"].mean()),
            "평균이익": avg_w,
            "평균손실": avg_l,
            "손익비": float(abs(avg_w / avg_l)) if avg_l < -1e-9 else 0.0,
            "기대값": float(trades["ret_net"].mean()),
            "평균보유일": float(trades["hold_days"].mean()),
            "최대이익": float(trades["ret_net"].max()),
            "최대손실": float(trades["ret_net"].min()),
        })
    return out


def yearly(equity: pd.Series) -> pd.Series:
    if equity is None or len(equity) < 2:
        return pd.Series(dtype=float)
    return equity.resample("YE").last().pct_change().fillna(
        equity.resample("YE").last().iloc[0] / equity.iloc[0] - 1.0
    )


def exit_mix(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or not len(trades):
        return pd.DataFrame()
    g = trades.groupby("reason").agg(
        건수=("ret_net", "size"),
        평균수익=("ret_net", "mean"),
        승률=("ret_net", lambda s: (s > 0).mean()),
    )
    return g.sort_values("건수", ascending=False)


def score_buckets(trades: pd.DataFrame, n: int = 4) -> pd.DataFrame:
    """점수 구간별 성과 — 점수가 실제로 변별력이 있는지 확인."""
    if trades is None or len(trades) < n * 5:
        return pd.DataFrame()
    t = trades.copy()
    try:
        t["bucket"] = pd.qcut(t["score"], n, labels=[f"Q{i+1}" for i in range(n)],
                              duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    return t.groupby("bucket", observed=True).agg(
        건수=("ret_net", "size"),
        평균수익=("ret_net", "mean"),
        승률=("ret_net", lambda s: (s > 0).mean()),
        평균점수=("score", "mean"),
    )


# ── IC (정보계수) ─────────────────────────────────────────────────────
def _wide(panel: dict[str, pd.DataFrame], col: str) -> pd.DataFrame:
    ser = {tk: df[col] for tk, df in panel.items() if col in df.columns}
    if not ser:
        return pd.DataFrame()
    return pd.DataFrame(ser)


def forward_return(panel: dict[str, pd.DataFrame], h: int) -> pd.DataFrame:
    """t일 종가 → t+h일 종가 수익률. t 시점에서는 미래값이므로 IC 측정에만 쓴다."""
    close = _wide(panel, "close")
    return close.shift(-h) / close - 1.0


def factor_ic(
    panel: dict[str, pd.DataFrame],
    factors: list[str],
    horizons: tuple[int, ...] = (5, 10, 20),
    min_names: int = 30,
) -> pd.DataFrame:
    """축별 예측력. 매일 횡단면 순위상관을 구한 뒤 평균과 t값을 낸다.

    평균 IC 0.02~0.03이면 쓸 만하고, t값 2 미만이면 우연과 구분이 안 된다.
    """
    fwd = {h: forward_return(panel, h) for h in horizons}
    rows = []
    for f in factors:
        W = _wide(panel, f)
        if W.empty:
            continue
        for h, F in fwd.items():
            common_idx = W.index.intersection(F.index)
            common_col = W.columns.intersection(F.columns)
            if len(common_idx) == 0 or len(common_col) == 0:
                continue
            a = W.loc[common_idx, common_col]
            b = F.loc[common_idx, common_col]
            ics = []
            for d in common_idx:
                x, y = a.loc[d], b.loc[d]
                m = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
                if m.sum() < min_names:
                    continue
                if x[m].nunique() < 5:
                    continue
                ics.append(x[m].corr(y[m], method="spearman"))
            if len(ics) < 20:
                continue
            arr = np.array(ics, dtype=float)
            arr = arr[np.isfinite(arr)]
            if len(arr) < 20:
                continue
            mean = arr.mean()
            t = mean / (arr.std(ddof=1) / np.sqrt(len(arr))) if arr.std(ddof=1) > 0 else 0.0
            rows.append({"factor": f, "기간": f"{h}일", "평균IC": mean,
                         "t값": t, "IC>0비율": float((arr > 0).mean()),
                         "표본일수": len(arr)})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["기간", "평균IC"], ascending=[True, False])


def signal_ic(
    panel: dict[str, pd.DataFrame],
    sig: dict[str, pd.DataFrame],
    horizons: tuple[int, ...] = (5, 10, 20),
) -> dict:
    """신호가 난 종목들만 놓고, 점수와 이후 수익률의 상관 + 평균 수익."""
    close = _wide(panel, "close")
    recs = {h: [] for h in horizons}
    for tk, s in sig.items():
        if tk not in close.columns:
            continue
        m = s["signal"].values.astype(bool)
        if not m.any():
            continue
        dates = s.index[m]
        scores = s["score"].values[m]
        c = close[tk]
        for h in horizons:
            f = c.shift(-h) / c - 1.0
            v = f.reindex(dates).values
            for sc, r in zip(scores, v):
                if np.isfinite(r):
                    recs[h].append((sc, r))
    out = {}
    for h, rr in recs.items():
        if len(rr) < 30:
            out[h] = {"n": len(rr)}
            continue
        df = pd.DataFrame(rr, columns=["score", "fwd"])
        out[h] = {
            "n": len(df),
            "평균수익": float(df["fwd"].mean()),
            "중앙값": float(df["fwd"].median()),
            "승률": float((df["fwd"] > 0).mean()),
            "점수상관": float(df["score"].corr(df["fwd"], method="spearman")),
            "상위25%평균": float(df[df["score"] >= df["score"].quantile(0.75)]["fwd"].mean()),
            "하위25%평균": float(df[df["score"] <= df["score"].quantile(0.25)]["fwd"].mean()),
        }
    return out


# ── 선택력 진단 ───────────────────────────────────────────────────────
# ── 축별 점수 (SWING_TASK_AXIS_SCORE.md 3절) — 실전 daily_scan 과 같은 정의 ─────
# stock_bot/swing/axes.py 를 그대로 쓴다(정의·방향 한 곳). 백테스트에는 DART 과거 접수일과
# 프로그램매매 데이터가 없어 quality/growth/prog 는 NULL 로 둔다(fiscal 날짜로 억지 백필 안 함).
def _axes_mod():
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from stock_bot.swing import axes  # noqa: WPS433
    return axes


def axis_materials(panel: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """축 재료 wide(date×ticker). flow5/20 은 원본 forgn/inst 가 창 안에 있을 때만 값(없으면 NaN)."""
    out = {c: _wide(panel, c) for c in ("per", "pbr", "value_ma20", "mktcap_eok")}
    fg, it = _wide(panel, "forgn"), _wide(panel, "inst")
    if not fg.empty and not it.empty:
        fg, it = fg.align(it, join="outer")
        raw = fg.fillna(0.0) + it.fillna(0.0)
        has = (fg.notna() | it.notna()).astype(float)
        for n in (5, 20):
            tot = raw.rolling(n, min_periods=1).sum()
            ok = has.rolling(n, min_periods=1).max() > 0
            out[f"flow{n}"] = tot.where(ok)
    return out


def axis_table(panel: dict[str, pd.DataFrame],
               sigs: dict[str, dict[str, pd.DataFrame]]) -> pd.DataFrame:
    """전략별 신호 → (date, ticker, strategy) 롱테이블 + 재료 + 축 점수.
    백분위 모집단 = 같은 날 신호 난 종목 전체(전략 무관) — daily_scan.add_ranks 와 동일."""
    AX = _axes_mod()
    recs = []
    for name, sig in sigs.items():
        for tk, s in sig.items():
            m = s["signal"].values.astype(bool)
            if m.any():
                for d in s.index[m]:
                    recs.append((d, tk, name))
    df = pd.DataFrame(recs, columns=["date", "ticker", "strategy"])
    if df.empty:
        for c in AX.AXIS_COLS:
            df[c] = np.nan
        return df
    key = pd.MultiIndex.from_arrays([df["date"], df["ticker"]])
    for c, W in axis_materials(panel).items():
        if W.empty:
            df[c] = np.nan
            continue
        df[c] = W.stack(dropna=False).reindex(key).values
    for c in ("roe", "debt", "qrev", "qinc", "prog5"):     # bt 에 재료 없음 → NULL
        df[c] = np.nan
    parts = [AX.compute(g, key="ticker") for _, g in df.groupby("date", sort=True)]
    return pd.concat(parts).reset_index(drop=True)


def axis_quintiles(panel: dict[str, pd.DataFrame], ax: pd.DataFrame,
                   h: int = 20, n_bucket: int = 5) -> pd.DataFrame:
    """축별 Q1~Q5(낮음→높음) 의 h일 수익률·초과수익 표. 축이 비면(NULL) n 만 찍힌다."""
    AX = _axes_mod()
    if ax.empty:
        return pd.DataFrame()
    close = _wide(panel, "close")
    fwd = close.shift(-h) / close - 1.0
    bench = fwd.mean(axis=1)
    key = pd.MultiIndex.from_arrays([ax["date"], ax["ticker"]])
    d = ax.copy()
    d["ret"] = fwd.stack(dropna=False).reindex(key).values
    d["bench"] = bench.reindex(d["date"]).values
    d["excess"] = d["ret"] - d["bench"]
    d = d[np.isfinite(d["ret"]) & np.isfinite(d["bench"])]
    rows = []
    for a in AX.AXES + ["total"]:
        col = f"{a}_score"
        sub = d[d[col].notna()]
        if len(sub) < n_bucket * 10:
            rows.append({"축": a, "n": len(sub)})
            continue
        try:
            q = pd.qcut(sub[col], n_bucket, labels=[f"Q{i+1}" for i in range(n_bucket)], duplicates="drop")
        except ValueError:
            rows.append({"축": a, "n": len(sub)})
            continue
        g = sub.groupby(q, observed=True)
        row = {"축": a, "n": len(sub)}
        for lab, gg in g:
            row[f"{lab}수익"] = float(gg["ret"].mean())
            row[f"{lab}초과"] = float(gg["excess"].mean())
        labs = list(g.groups)
        if len(labs) >= 2:
            row["Q5-Q1초과"] = row[f"{labs[-1]}초과"] - row[f"{labs[0]}초과"]
        rows.append(row)
    return pd.DataFrame(rows)


def selection_report(
    panel: dict[str, pd.DataFrame],
    sig: dict[str, pd.DataFrame],
    horizons: tuple[int, ...] = (5, 10, 20),
    fund_cols: tuple[str, ...] = ("per", "pbr", "value_eok", "mktcap_eok"),
    n_bucket: int = 5,
    axis_scores: pd.DataFrame | None = None,
) -> dict:
    """'고른 종목이 시장 평균보다 나았나'만 잰다. 청산·비용은 일부러 배제.

    핵심은 초과수익(alpha) = 신호 종목 수익률 - 같은 날 유니버스 평균 수익률.
    강세장이면 아무거나 사도 플러스가 나오므로, 벤치마크를 빼지 않으면
    선택력인지 시장 덕인지 구분되지 않는다.

    axis_scores(axis_table 결과)를 주면 신호 테이블에 축별 점수 컬럼을 붙이고(기록용),
    res["축분위"] 에 축별 Q1~Q5 초과수익을 넣는다. 전략 조건·점수 산식은 건드리지 않는다.

    t값은 같은 날 신호들을 하나로 묶어 '일자 단위'로 계산한다. 종목 단위로
    세면 같은 날 신호가 서로 독립인 것처럼 취급돼 t값이 크게 부풀려진다.
    (보유기간이 겹치는 문제까지는 보정하지 않으므로 여전히 낙관적이다.)
    """
    close = _wide(panel, "close")
    if close.empty:
        return {}
    fwd = {h: close.shift(-h) / close - 1.0 for h in horizons}
    bench = {h: f.mean(axis=1) for h, f in fwd.items()}     # 동일가중 유니버스 평균

    fund = {c: _wide(panel, c) for c in fund_cols}

    out: dict = {}
    for h in horizons:
        F, B = fwd[h], bench[h]
        recs = []
        for tk, s in sig.items():
            if tk not in F.columns:
                continue
            m = s["signal"].values.astype(bool)
            if not m.any():
                continue
            dates = s.index[m]
            scores = s["score"].values[m]
            r = F[tk].reindex(dates).values
            b = B.reindex(dates).values
            for i, d in enumerate(dates):
                if not (np.isfinite(r[i]) and np.isfinite(b[i])):
                    continue
                row = {"date": d, "ticker": tk, "score": scores[i],
                       "ret": r[i], "bench": b[i], "excess": r[i] - b[i]}
                for c, W in fund.items():
                    row[c] = (W.at[d, tk] if (tk in W.columns and d in W.index)
                              else np.nan)
                recs.append(row)
        if len(recs) < 30:
            out[h] = {"n": len(recs)}
            continue
        df = pd.DataFrame(recs)
        if axis_scores is not None and not axis_scores.empty:
            AX = _axes_mod()
            df["setup_score"] = df["score"]
            df["setup_pscore"] = df.groupby("date")["score"].rank(pct=True) * 100   # 전략 내(같은 날)
            a = axis_scores[["date", "ticker", *AX.AXIS_COLS]].drop_duplicates(["date", "ticker"])
            df = df.merge(a, on=["date", "ticker"], how="left")

        # 일자 단위 집계로 t값
        daily = df.groupby("date")["excess"].mean()
        t = (daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily)))
             if len(daily) > 2 and daily.std(ddof=1) > 0 else 0.0)

        res = {
            "n": len(df),
            "신호일수": int(len(daily)),
            "신호평균": float(df["ret"].mean()),
            "유니버스평균": float(df["bench"].mean()),
            "초과수익": float(df["excess"].mean()),
            "t값": float(t),
            "시장대비승률": float((df["excess"] > 0).mean()),
            "절대승률": float((df["ret"] > 0).mean()),
            "중앙값초과": float(df["excess"].median()),
        }

        # 점수 분위별 초과수익 — 스코어링 변별력
        try:
            df["q"] = pd.qcut(df["score"], n_bucket,
                              labels=[f"Q{i+1}" for i in range(n_bucket)],
                              duplicates="drop")
            qq = df.groupby("q", observed=True).agg(
                건수=("excess", "size"), 초과수익=("excess", "mean"),
                평균점수=("score", "mean"))
            res["점수분위"] = qq
            if len(qq) >= 2:
                res["스프레드"] = float(qq["초과수익"].iloc[-1] - qq["초과수익"].iloc[0])
        except ValueError:
            pass

        # 재무 분위별 초과수익 — 가점으로 쓸 값어치가 있는지
        fb = {}
        for c in fund_cols:
            sub = df[df[c].notna() & np.isfinite(df[c]) & (df[c] > 0)]
            if len(sub) < n_bucket * 10:
                continue
            try:
                sub = sub.copy()
                sub["fq"] = pd.qcut(sub[c], n_bucket,
                                    labels=[f"{i+1}" for i in range(n_bucket)],
                                    duplicates="drop")
                fb[c] = sub.groupby("fq", observed=True).agg(
                    건수=("excess", "size"), 초과수익=("excess", "mean"),
                    평균값=(c, "mean"))
            except ValueError:
                continue
        if fb:
            res["재무분위"] = fb

        # 축별 분위 초과수익 (기록용)
        if axis_scores is not None and "total_score" in df.columns:
            AX = _axes_mod()
            ab = {}
            for a_ in AX.AXES + ["total"]:
                col = f"{a_}_score"
                sub = df[df[col].notna()]
                if len(sub) < n_bucket * 10:
                    continue
                try:
                    sub = sub.copy()
                    sub["aq"] = pd.qcut(sub[col], n_bucket,
                                        labels=[f"Q{i+1}" for i in range(n_bucket)], duplicates="drop")
                    ab[a_] = sub.groupby("aq", observed=True).agg(
                        건수=("excess", "size"), 수익=("ret", "mean"), 초과수익=("excess", "mean"))
                except ValueError:
                    continue
            if ab:
                res["축분위"] = ab
            res["신호테이블"] = df
        out[h] = res
    return out


# ── 출력 ─────────────────────────────────────────────────────────────
def pct(x: float, d: int = 2) -> str:
    return f"{x*100:+.{d}f}%" if np.isfinite(x) else "  n/a"


def summary_table(results: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for name, r in results.items():
        p = r.get("perf", {})
        if not p:
            continue
        rows.append({
            "전략": name,
            "거래수": p.get("거래수", 0),
            "CAGR": p.get("CAGR", np.nan),
            "MDD": p.get("MDD", np.nan),
            "샤프": p.get("샤프", np.nan),
            "칼마": p.get("칼마", np.nan),
            "승률": p.get("승률", np.nan),
            "손익비": p.get("손익비", np.nan),
            "평균수익": p.get("평균수익", np.nan),
            "평균보유일": p.get("평균보유일", np.nan),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values("칼마", ascending=False)
