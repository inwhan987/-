# -*- coding: utf-8 -*-
"""스윙 "저항 이평(60/120일선) 조건부 청산" 검증 — 분석 전용 (2026-09-28).

라이브 코드/전략 조건식/.env.overrides 는 건드리지 않는다. 읽기만 한다:
  - 신호·경로 패널: 기존 고속 백테스트 하네스(swbt/signals*.pkl, grid.simulate 와 같은 비용·체결 규칙)
  - 저항선(ma60/ma120): data/swing.db 의 일봉 종가로 직접 계산 후 **shift(1)** — 항상 전일까지의 값

규칙
  A  현행         손절 20% + 타임스톱 30일
  B  단순 트레일   A + 트레일(무장 고가≥진입×(1+after), 발동 종가≤peak×(1-pct))
  C  저항 조건부   R = 전일 ma60/ma120 중 당일 시가 위 최소값 (매일 갱신, 없으면 A와 동일)
                  터치 고가≥R×0.99 / 막힘(종가<R) → 당일 종가 청산 / 돌파(종가>R) → 이후 고점대비 -8% 트레일
  C2 막힘 절반청산 + 나머지 고점대비 -5% 트레일
  D  진입 필터     진입가 위 3% 안에 저항선이 있으면 진입 안 함 (C와 조합)

공통: 손절 체결 = min(시가, stop_px) (엔진과 동일, 모든 규칙에 같이 적용)
사용: PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/research/resistance_exit.py
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SW = os.environ.get("SWBT", r"C:\Users\lainw\AppData\Local\Temp\claude"
                            r"\C--Users-lainw-Desktop-------\20929921-b900-47a1-b239-7ec5e60260ee\scratchpad\swbt")
CACHE = os.path.join(os.environ.get("TEMP", "."), "res_panel.pkl")
sys.path.insert(0, SW)
import grid  # noqa: E402  (하네스 — 비용·진입모델 재사용)
import ms1  # noqa: E402

ACT8 = ["NEWHIGH", "MOMENTUM", "FLOW_PULLBACK", "FLOW_FORGN", "FLOW_INST", "FLOW_BOTH", "MEANREV", "GAPGO"]
SPLIT = "20250701"          # 기존 검증(2026-09-21~27)과 동일한 IS/OOS 경계
SLOTS, PER_DAY, SEEDS = 10, 3, 20
BUY, SELL = grid.BUY_COST, grid.SELL_COST
RL = {1: "손절", 3: "트레일", 4: "타임스톱", 6: "데이터끝", 7: "막힘"}


# ─────────────────────────── 데이터 ───────────────────────────
def load():
    ms1.ACTIVE = ACT8
    sig, P = ms1.load_all()
    m = ms1.gate(sig)
    sig, P = sig[m].reset_index(drop=True), P[m]
    ent = ms1.entries(sig, P)
    return sig, P, ent, sig["avail"].to_numpy()


def build_res(sig, H):
    """(n, H, 2) = 경로 각 날짜의 **전일 기준** ma60 / ma120. swing.db 일봉 종가로 계산."""
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            D = pickle.load(f)
        if D["key"] == (len(sig), H, str(sig["date"].iloc[-1])):
            return D["res"], D["ent_lines"]
    db = os.path.join(REPO, "data", "swing.db")
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    res = np.full((len(sig), H, 2), np.nan, dtype=np.float32)
    ent_lines = np.full((len(sig), 2), np.nan, dtype=np.float32)   # 진입일(전일값) ma60/ma120
    for code, g in sig.groupby("code"):
        px = pd.read_sql_query("SELECT date,close FROM daily WHERE code=? ORDER BY date", c,
                               params=(code,), index_col="date")
        if px.empty:
            continue
        px.index = pd.to_datetime(px.index, format="%Y%m%d")
        cl = px["close"].astype(float)
        m60 = cl.rolling(60, min_periods=60).mean().shift(1).to_numpy()
        m120 = cl.rolling(120, min_periods=120).mean().shift(1).to_numpy()
        pos = {d: i for i, d in enumerate(px.index)}
        n = len(cl)
        for ridx, d0 in zip(g.index.to_numpy(), g["date"].to_numpy()):
            i = pos.get(pd.Timestamp(d0))
            if i is None:
                continue
            j = min(n, i + 1 + H)
            k = j - (i + 1)
            if k <= 0:
                continue
            res[ridx, :k, 0] = m60[i + 1:j]
            res[ridx, :k, 1] = m120[i + 1:j]
            ent_lines[ridx] = (m60[i + 1], m120[i + 1])
    c.close()
    with open(CACHE, "wb") as f:
        pickle.dump({"key": (len(sig), H, str(sig["date"].iloc[-1])), "res": res, "ent_lines": ent_lines}, f)
    return res, ent_lines


def line_above(a, b, ref):
    """ref(당일 시가) 위에 있는 두 선 중 최소값. 없으면 NaN."""
    A = np.where(np.isfinite(a) & (a > ref), a, np.inf)
    B = np.where(np.isfinite(b) & (b > ref), b, np.inf)
    R = np.minimum(A, B)
    return np.where(np.isinf(R), np.nan, R)


def netpx(px, ent):
    return (px * (1 - SELL)) / (ent * (1 + BUY)) - 1


# ─────────────────────────── 시뮬레이터 ───────────────────────────
def sim(P, RES, ent, avail, stop=0.20, tdays=30, trail_after=0.0, trail_pct=0.0,
        resist=False, block_frac=1.0, break_trail=0.08, rest_trail=0.05):
    """일자축 벡터 시뮬. 반환 net, hold, reason, blocked(막힘 발생), broke(돌파 발생)."""
    n, H, _ = P.shape
    alive = np.isfinite(ent)
    net = np.full(n, np.nan)
    hold = np.zeros(n, int)
    reason = np.zeros(n, int)
    part = np.zeros(n)                 # 부분청산 실현분 (비중 가중 net)
    pdone = np.zeros(n, bool)
    blocked = np.zeros(n, bool)
    broke = np.zeros(n, bool)
    stop_px = ent * (1 - stop)
    peak = ent.copy()
    trail_on = np.zeros(n, bool)
    for d in range(H):
        o, h, l, c = P[:, d, 0], P[:, d, 1], P[:, d, 2], P[:, d, 3]
        cur = alive & (d < avail)
        if not cur.any():
            break
        broke_prev, pdone_prev = broke.copy(), pdone.copy()
        px = np.full(n, np.nan)
        rs = np.zeros(n, int)
        # 손절: 갭이면 시가(= min(시가, stop_px)), 아니면 손절가. 진입 당일은 갭 판정 없음
        m = cur & (d > 0) & (o <= stop_px)
        px[m], rs[m] = o[m], 1
        m = cur & (rs == 0) & (l <= stop_px)
        px[m], rs[m] = stop_px[m], 1
        peak = np.where(cur, np.fmax(peak, h), peak)
        tpct = np.zeros(n)
        if resist:
            R = line_above(RES[:, d, 0], RES[:, d, 1], o)
            hasR = np.isfinite(R)
            touch = hasR & (h >= R * 0.99)
            blk = cur & (rs == 0) & touch & (c < R) & ~broke_prev
            blocked |= blk
            if block_frac >= 1.0:
                px[blk], rs[blk] = c[blk], 7
            else:                                    # C2: 절반만 청산, 나머지는 트레일 유지
                half = blk & ~pdone_prev
                part[half] += block_frac * netpx(c[half], ent[half])
                pdone |= half
            broke |= cur & hasR & (c > R)
            tpct = np.where(pdone_prev, rest_trail, np.where(broke_prev, break_trail, 0.0))
        elif trail_pct > 0:
            trail_on |= cur & (h >= ent * (1 + trail_after))
            tpct = np.where(trail_on, trail_pct, 0.0)
        m = cur & (rs == 0) & (tpct > 0) & (c <= peak * (1 - tpct))
        px[m], rs[m] = c[m], 3
        if tdays > 0:
            m = cur & (rs == 0) & (d + 1 >= tdays)
            px[m], rs[m] = c[m], 4
        m = cur & (rs == 0) & (d + 1 >= avail)       # 패널 끝 = 마지막 종가 평가청산
        px[m], rs[m] = c[m], 6
        done = cur & (rs > 0)
        rem = np.where(pdone, 1.0 - block_frac, 1.0)
        net[done] = part[done] + rem[done] * netpx(px[done], ent[done])
        hold[done] = d + 1
        reason[done] = rs[done]
        alive = alive & ~done
    return net, hold, reason, blocked, broke


# ─────────────────────────── 지표 ───────────────────────────
def trade_stats(net, hold, reason, idx):
    a = net[idx]
    a = a[np.isfinite(a)]
    if len(a) < 5:
        return None
    w, lo = a[a > 0], a[a <= 0]
    aw = w.mean() if len(w) else 0.0
    al = lo.mean() if len(lo) else 0.0
    rr = aw / abs(al) if al < 0 else float("inf")
    mix = {}
    for k in sorted(set(reason[idx])):
        mix[RL.get(k, str(k))] = (reason[idx] == k).mean() * 100
    return dict(n=len(a), win=(a > 0).mean() * 100, avgwin=aw * 100, avgloss=al * 100, rr=rr,
                exp=a.mean() * 100, hold=hold[idx].mean(), mix=mix)


def port_stats(sub, ent, avail, net, hold, reason):
    """슬롯 10 · 하루 3건 · 무작위 랭킹 시드 20개 중앙값."""
    rows = []
    ym = pd.to_datetime(sub["date"]).dt.strftime("%Y-%m").to_numpy()
    for s in range(SEEDS):
        t, _ = ms1.portfolio(sub, ent, avail, net, hold, reason, slots=SLOTS, per_day=PER_DAY,
                             rank="random", seed=s)
        if len(t) < 10:
            continue
        a = net[t]
        w, lo = a[a > 0], a[a <= 0]
        pf = w.sum() / -lo.sum() if lo.sum() < 0 else float("inf")
        order = np.argsort(pd.to_datetime(sub["date"]).to_numpy()[t], kind="stable")
        eq = np.cumsum(a[order] * 100 / SLOTS)
        mdd = float((eq - np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]).min())
        mon = pd.Series(a * 100 / SLOTS).groupby(ym[t]).sum()
        rows.append((a.sum() * 100 / SLOTS, pf, mdd, (mon > 0).mean() * 100, mon.min(), len(t)))
    if not rows:
        return None
    M = np.array(rows, dtype=float)
    med = np.median(M, axis=0)
    return dict(total=med[0], pf=med[1], mdd=med[2], mwin=med[3], worst=med[4], n=med[5])


# ─────────────────────────── 1단계 ───────────────────────────
def stage1(sig, P, RES, ent_lines, ent, avail, out):
    net, hold, reason, _, _ = sim(P, RES, ent, avail)
    ok = np.isfinite(net)
    n = len(ent)
    mfe = np.full(n, np.nan)
    mfe_day = np.zeros(n, int)
    near = np.zeros(n, bool)
    near_rnd = np.zeros(n, bool)
    touched = np.zeros(n, bool)
    brk = np.zeros(n, bool)
    rng = np.random.default_rng(7)
    for i in np.flatnonzero(ok):
        H = hold[i]
        h = P[i, :H, 1]
        o = P[i, :H, 0]
        c = P[i, :H, 3]
        r = h / ent[i] - 1
        k = int(np.nanargmax(r))
        mfe[i], mfe_day[i] = r[k], k + 1
        a, b = RES[i, :H, 0], RES[i, :H, 1]

        def close_to(d):
            return bool(((np.abs(h[d] / a[d] - 1) <= 0.02) if np.isfinite(a[d]) else False)
                        or ((np.abs(h[d] / b[d] - 1) <= 0.02) if np.isfinite(b[d]) else False))

        near[i] = close_to(k)
        near_rnd[i] = close_to(int(rng.integers(0, H)))
        R = line_above(a, b, o)
        t = np.isfinite(R) & (h >= R * 0.99)
        touched[i] = t.any()
        brk[i] = bool((np.isfinite(R) & (c > R)).any())

    e = ent[ok]
    above = (np.isfinite(ent_lines[:, 0]) & (ent_lines[:, 0] > ent))[ok] | \
            (np.isfinite(ent_lines[:, 1]) & (ent_lines[:, 1] > ent))[ok]
    near3 = ((np.isfinite(ent_lines[:, 0]) & (ent_lines[:, 0] > ent) & (ent_lines[:, 0] <= ent * 1.03)) |
             (np.isfinite(ent_lines[:, 1]) & (ent_lines[:, 1] > ent) & (ent_lines[:, 1] <= ent * 1.03)))[ok]
    st = sig["strategy"].to_numpy()[ok]
    nn, mm, md = net[ok], mfe[ok], mfe_day[ok]
    nr, nrr, tc, bk = near[ok], near_rnd[ok], touched[ok], brk[ok]

    out.append("\n### 1단계 (규칙 A 기준) — 전체")
    out.append("| 구분 | n | MFE≥5% 비율 | 그중 손실마감 | MFE≥10% 비율 | 그중 손실마감 | MFE일 중앙 | 저항±2% (MFE일) | 저항±2% (무작위일) |")
    out.append("|---|---|---|---|---|---|---|---|---|")

    def row1(lab, m):
        if m.sum() < 20:
            out.append(f"| {lab} | {m.sum()} | 표본부족 | | | | | | |")
            return
        f5, f10 = mm[m] >= 0.05, mm[m] >= 0.10
        l5 = (nn[m][f5] < 0).mean() * 100 if f5.sum() else float("nan")
        l10 = (nn[m][f10] < 0).mean() * 100 if f10.sum() else float("nan")
        out.append(f"| {lab} | {m.sum()} | {f5.mean()*100:.1f}% | {l5:.1f}% | {f10.mean()*100:.1f}% | {l10:.1f}% | "
                   f"{np.median(md[m]):.0f}일 | {nr[m].mean()*100:.1f}% | {nrr[m].mean()*100:.1f}% |")

    row1("전체", np.ones(len(nn), bool))
    for s in ACT8:
        row1(s, st == s)

    out.append("\n### 1단계-3 돌파 vs 막힘 (보유 중 저항선 터치한 거래)")
    out.append("| 구분 | 터치 n | 돌파 n | 돌파 평균 | 돌파 중앙 | 돌파 승률 | 막힘 n | 막힘 평균 | 막힘 중앙 | 막힘 승률 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")

    def row3(lab, m):
        t = m & tc
        b, k = t & bk, t & ~bk
        if t.sum() < 20:
            out.append(f"| {lab} | {t.sum()} | 표본부족 | | | | | | | |")
            return
        out.append(f"| {lab} | {t.sum()} | {b.sum()} | {nn[b].mean()*100:+.2f}% | {np.median(nn[b])*100:+.2f}% | "
                   f"{(nn[b]>0).mean()*100:.1f}% | {k.sum()} | {nn[k].mean()*100:+.2f}% | "
                   f"{np.median(nn[k])*100:+.2f}% | {(nn[k]>0).mean()*100:.1f}% |")

    row3("전체", np.ones(len(nn), bool))
    for s in ACT8:
        row3(s, st == s)

    out.append("\n### 1단계-4 진입 시점에 진입가 위 저항선")
    out.append("| 구분 | n | 위에 저항선 있음 | 3% 안에 있음 |")
    out.append("|---|---|---|---|")
    out.append(f"| 전체 | {len(nn)} | {above.mean()*100:.1f}% | {near3.mean()*100:.1f}% |")
    for s in ACT8:
        m = st == s
        if m.sum():
            out.append(f"| {s} | {m.sum()} | {above[m].mean()*100:.1f}% | {near3[m].mean()*100:.1f}% |")
    return dict(net=net, hold=hold, reason=reason, mfe=mfe, touched=touched, brk=brk)


# ─────────────────────────── 2단계 ───────────────────────────
def rules():
    R = [("A 현행", dict())]
    for ta in (0.05, 0.08):
        for tp in (0.04, 0.06):
            R.append((f"B trail{int(ta*100)}>{int(tp*100)}", dict(trail_after=ta, trail_pct=tp)))
    R.append(("C 저항조건부", dict(resist=True)))
    R.append(("C2 절반청산", dict(resist=True, block_frac=0.5)))
    return R


def main():
    out = []
    sig, P, ent, avail = load()
    H = P.shape[1]
    RES, ent_lines = build_res(sig, H)
    out.append(f"게이트 통과 신호 {len(sig)}건 / 트리거 진입 {np.isfinite(ent).sum()}건 · "
               f"{sig['date'].min():%Y-%m-%d}~{sig['date'].max():%Y-%m-%d} · IS<{SPLIT}≤OOS · "
               f"패널 H={H}거래일 · 슬롯{SLOTS}/일{PER_DAY}건/시드{SEEDS} 중앙값")
    out.append("진입 분포: " + "  ".join(f"{k} {v}" for k, v in
                                      sig.loc[np.isfinite(ent), "strategy"].value_counts().items()))
    s1 = stage1(sig, P, RES, ent_lines, ent, avail, out)

    # D 진입 필터 (진입가 위 3% 안 저항선 → 진입 제외)
    d_block = ((np.isfinite(ent_lines[:, 0]) & (ent_lines[:, 0] > ent) & (ent_lines[:, 0] <= ent * 1.03)) |
               (np.isfinite(ent_lines[:, 1]) & (ent_lines[:, 1] > ent) & (ent_lines[:, 1] <= ent * 1.03)))
    ent_d = ent.copy()
    ent_d[d_block] = np.nan

    RUNS = []
    for lab, kw in rules():
        RUNS.append((lab, ent, sim(P, RES, ent, avail, **kw)))
    RUNS.append(("C+D 진입필터", ent_d, sim(P, RES, ent_d, avail, resist=True)))
    RUNS.append(("C2+D 진입필터", ent_d, sim(P, RES, ent_d, avail, resist=True, block_frac=0.5)))

    date = pd.to_datetime(sig["date"])
    IS = (date < SPLIT).to_numpy()
    OOS = ~IS
    out.append("\n### 2단계 규칙 비교 (전체 전략 합산)")
    out.append("| 규칙 | 구간 | n | 승률 | 평균익 | 평균손 | 손익비 | 기대값 | 총수익 | PF | MDD | 월승률 | 최악월 | 보유 | 청산사유 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    table = {}
    for lab, e, (net, hold, reason, blocked, broke) in RUNS:
        for per, m in (("IS", IS), ("OOS", OOS)):
            idx = np.flatnonzero(m & np.isfinite(net))
            ts = trade_stats(net, hold, reason, idx)
            sub = sig[m].reset_index(drop=True)
            ps = port_stats(sub, e[m], avail[m], net[m], hold[m], reason[m])
            table[(lab, per)] = (ts, ps)
            if not ts or not ps:
                out.append(f"| {lab} | {per} | 표본부족 | | | | | | | | | | | | |")
                continue
            mix = " ".join(f"{k}{v:.0f}%" for k, v in ts["mix"].items())
            if blocked[m].any() and lab.startswith("C2"):
                mix += f" (막힘반청산 {blocked[idx].mean()*100:.0f}%)"
            out.append(f"| {lab} | {per} | {ts['n']} | {ts['win']:.1f}% | {ts['avgwin']:+.2f}% | {ts['avgloss']:+.2f}% | "
                       f"{ts['rr']:.2f} | {ts['exp']:+.2f}% | {ps['total']:+.0f}% | {ps['pf']:.2f} | {ps['mdd']:.0f}% | "
                       f"{ps['mwin']:.0f}% | {ps['worst']:+.1f}% | {ts['hold']:.1f}일 | {mix} |")

    out.append("\n### 2단계 전략별 (거래 단위, OOS)")
    out.append("| 전략 | 규칙 | n | 승률 | 손익비 | 기대값 | 보유 |")
    out.append("|---|---|---|---|---|---|---|")
    for s in ACT8:
        for lab, e, (net, hold, reason, _b, _k) in RUNS:
            idx = np.flatnonzero(OOS & (sig["strategy"].to_numpy() == s) & np.isfinite(net))
            ts = trade_stats(net, hold, reason, idx)
            if not ts:
                continue
            out.append(f"| {s} | {lab} | {ts['n']} | {ts['win']:.1f}% | {ts['rr']:.2f} | {ts['exp']:+.2f}% | {ts['hold']:.1f}일 |")

    # 민감도: 채택 기준(7절)을 OOS 에서 통과한 단 하나의 규칙에만 — 승률·최악월 개선 + 기대값 최대인 B 조합
    best = "B trail8>6"
    kw = dict(rules()).get(best, dict(resist=True) if best.startswith("C") else dict())
    out.append(f"\n### 민감도 (규칙 `{best}` 만)")
    out.append("| 변형 | 구간 | n | 승률 | 손익비 | 기대값 | 총수익 | MDD | 최악월 | 보유 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    e = ent_d if best.endswith("진입필터") else ent
    for slab, extra in (("기본 손절20/타임30", {}), ("손절15%", dict(stop=0.15)),
                        ("타임스톱20일", dict(tdays=20)), ("타임스톱15일", dict(tdays=15))):
        net, hold, reason, _b, _k = sim(P, RES, e, avail, **{**kw, **extra})
        for per, m in (("IS", IS), ("OOS", OOS)):
            idx = np.flatnonzero(m & np.isfinite(net))
            ts = trade_stats(net, hold, reason, idx)
            ps = port_stats(sig[m].reset_index(drop=True), e[m], avail[m], net[m], hold[m], reason[m])
            if not ts or not ps:
                continue
            out.append(f"| {slab} | {per} | {ts['n']} | {ts['win']:.1f}% | {ts['rr']:.2f} | {ts['exp']:+.2f}% | "
                       f"{ps['total']:+.0f}% | {ps['mdd']:.0f}% | {ps['worst']:+.1f}% | {ts['hold']:.1f}일 |")

    txt = "\n".join(out)
    print(txt)
    dst = os.path.join(os.environ.get("TEMP", "."), "resistance_exit_out.md")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(txt)
    print("\n-> " + dst)


if __name__ == "__main__":
    main()
