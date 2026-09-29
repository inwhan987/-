# -*- coding: utf-8 -*-
"""다전략 합산 포트(슬롯10) 에서 타임스톱 단축 스윕 + 청산사유 분해.

사용자 질문: "슬롯이 만석이면 회전을 올려 슬롯을 더 잘 쓰는 게 낫지 않나".
기존 기각 근거는 단일전략·슬롯5(슬롯이 남던 상황)라 이 질문에 답이 안 된다.
여기서는 12전략을 한 바구니에 넣고 슬롯 만석 상태를 실제로 만든 뒤 잰다.
"""
from __future__ import annotations
import os, sys, pickle, numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# 신호 패널(.pkl, 4GB)은 git 에 못 넣는다 → 저장소 옆 고정 경로. 코드만 저장소에 있다.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
DATA = os.environ.get("SWBT", os.path.join(os.path.dirname(_REPO), "swbt"))
import grid
from grid import BUY_COST, SELL_COST, KIND, entry_model, simulate

CUT = {"PULLBACK": 80.0, "FLOW_FORGN": 70.0, "GAPGO": 80.0, "VALUE_MOM": 70.0, "VALUE_PURE": 70.0}
DEFAULT_CUT = 60.0
MIN_CAP = 5000.0          # 2026-09-23 적용값
ACTIVE = ["NEWHIGH", "MOMENTUM", "FLOW_PULLBACK", "FLOW_FORGN", "FLOW_INST", "FLOW_BOTH",
          "MEANREV", "GAPGO", "VALUE_MOM", "VALUE_PURE"]   # BREAKOUT/PULLBACK 은 비활성


def load_all():
    with open(os.path.join(DATA, "signals.pkl"), "rb") as f:
        D = pickle.load(f)
    sig, P = D["sig"], D["paths"]
    keep = ~sig["strategy"].isin(["VALUE_MOM", "VALUE_PURE"]).to_numpy()
    sig, P = sig[keep].reset_index(drop=True), P[keep]
    with open(os.path.join(DATA, "signals_val.pkl"), "rb") as f:
        DV = pickle.load(f)
    sv, PV = DV["sig"], DV["paths"]
    assert P.shape[1:] == PV.shape[1:], (P.shape, PV.shape)
    sig = pd.concat([sig, sv], ignore_index=True)
    P = np.concatenate([P, PV], axis=0)
    return sig, P


def gate(sig):
    cut = sig["strategy"].map(lambda s: CUT.get(s, DEFAULT_CUT)).to_numpy()
    m = (sig["avail"].to_numpy() >= 1)
    m &= sig["strategy"].isin(ACTIVE).to_numpy()
    m &= (sig["date"] >= pd.Timestamp("20240101")).to_numpy()
    m &= (sig["score"].to_numpy() >= cut)
    for col, lo, hi in (("value_eok", 30.0, None), ("mktcap_eok", MIN_CAP, None),
                        ("close", 1000.0, None), ("atr_pct", None, 0.07), ("regime", 1.0, None)):
        v = sig[col].to_numpy(dtype=float)
        m &= np.where(np.isnan(v), False, (v >= lo) if lo is not None else (v <= hi))
    return m


def entries(sig, P):
    ent = np.full(len(sig), np.nan)
    for st in sig["strategy"].unique():
        k = (sig["strategy"] == st).to_numpy()
        e, _ = entry_model(sig[k].reset_index(drop=True), P[k], KIND.get(st, "hold"), "trigger")
        ent[k] = e
    return ent


def portfolio(sig, ent, avail, net, hold, reason, slots=10, per_day=3, rank="score", seed=0):
    """날짜순으로 슬롯을 채운다. 반환 = 채택된 거래 인덱스 + 운용 통계."""
    dates = np.sort(sig["date"].unique())
    dpos = {d: i for i, d in enumerate(dates)}
    d1 = sig["date"].map(dpos).to_numpy()
    code = sig["code"].to_numpy()
    sc = sig["score"].to_numpy()
    if rank == "random":
        sc = np.random.default_rng(seed).random(len(sig))
    valid = np.isfinite(net) & (reason != 9) & np.isfinite(ent)
    idx = np.flatnonzero(valid)
    idx = idx[np.lexsort((-sc[idx], d1[idx]))]
    held = []; taken = []; hs = 0; cur = -1; n_today = 0; rejected = 0
    for i in idx:
        d = d1[i]
        if d != cur:
            cur = d; n_today = 0
            kept = []
            for p in held:
                if p[1] <= d:
                    hs += p[2]
                else:
                    kept.append(p)
            held = kept
        if n_today >= per_day or code[i] in {p[0] for p in held}:
            continue
        if len(held) >= slots:
            rejected += 1
            continue
        held.append((code[i], d + int(hold[i]), int(hold[i])))
        taken.append(i); n_today += 1
    for p in held:
        hs += p[2]
    return np.array(taken, dtype=int), dict(rejected=rejected, slot_days=hs,
                                            n_days=len(dates), util=hs / max(slots * len(dates), 1))


def summarize(sig, net, hold, reason, taken, meta, slots, label):
    a = net[taken]
    w, lo = a[a > 0], a[a <= 0]
    pf = w.sum() / -lo.sum() if lo.sum() < 0 else np.inf
    yrs = sig["date"].dt.year.to_numpy()[taken]
    by = " ".join(f"{y}:{a[yrs == y].sum()*100/slots:+.0f}" for y in sorted(set(yrs)))
    return (f"{label:<12} n={len(a):>5} 평균보유{hold[taken].mean():5.1f}일 승률{(a>0).mean()*100:5.1f}% "
            f"PF {pf:4.2f} 기대{a.mean()*100:+6.2f}% 누적{a.sum()*100/slots:+8.1f}% "
            f"이용{meta['util']*100:3.0f}% 만석기각{meta['rejected']:>5}  [{by}]")


def exit_mix(sig, net, hold, reason, taken, R):
    rows = []
    for k in sorted(set(reason[taken])):
        m = taken[reason[taken] == k]
        a = net[m]
        rows.append((R.get(k, str(k)), len(m), len(m) / len(taken) * 100,
                     a.mean() * 100, np.median(a) * 100, (a > 0).mean() * 100,
                     hold[m].mean(), a.sum() * 100))
    return rows


def main():
    sig, P = load_all()
    m = gate(sig)
    sig, P = sig[m].reset_index(drop=True), P[m]
    print(f"게이트 통과 신호 {len(sig)}건 (2024~, 시총{MIN_CAP:.0f}억, 전략별 원점수 컷 적용)")
    print(sig["strategy"].value_counts().to_string())
    ent = entries(sig, P)
    avail = sig["avail"].to_numpy()
    print(f"트리거 진입 {np.isfinite(ent).sum()}건 / {len(sig)}")
    SLOTS = 10
    print(f"\n===== 슬롯 {SLOTS} · 하루 신규 3 · 손절 20% 고정 · 진입 트리거모델 =====")
    print("--- [A] 타임스톱 스윕 (점수 랭킹) ---")
    store = {}
    for td in (10, 15, 20, 25, 30):
        gross, net, hold, reason, R = simulate(P, ent, avail, 0.20, 0, 0, 0, td, 0)
        taken, meta = portfolio(sig, ent, avail, net, hold, reason, slots=SLOTS)
        store[td] = (net, hold, reason, taken, meta, R)
        print("   " + summarize(sig, net, hold, reason, taken, meta, SLOTS, f"타임스톱{td}일"))
    print("--- [B] 같은 스윕, 무작위 랭킹 (대조군, 시드 3개 중앙) ---")
    for td in (10, 20, 30):
        gross, net, hold, reason, R = simulate(P, ent, avail, 0.20, 0, 0, 0, td, 0)
        outs = []
        for s in range(3):
            taken, meta = portfolio(sig, ent, avail, net, hold, reason, slots=SLOTS, rank="random", seed=s)
            outs.append((net[taken].sum() * 100 / SLOTS, meta["util"] * 100, meta["rejected"]))
        outs.sort()
        print(f"   타임스톱{td}일   누적 {outs[1][0]:+8.1f}%  이용{outs[1][1]:3.0f}%  만석기각{outs[1][2]:>5}")
    print("\n--- [C] 청산 사유 분해 (채택된 거래만, 손절 20% 고정) ---")
    for td in (10, 20, 30):
        net, hold, reason, taken, meta, R = store[td]
        print(f"   [타임스톱 {td}일]  사유          건수    비중   평균수익   중앙   승률   평균보유   수익합")
        for name, n, pct_, mean, med, win, hd, tot in exit_mix(sig, net, hold, reason, taken, R):
            print(f"                    {name:<8} {n:>6} {pct_:6.1f}% {mean:+8.2f}% {med:+7.2f}% {win:5.1f}% {hd:8.1f}일 {tot:+9.1f}%")
    print("\n--- [D] 손절을 안 했다면? (타임스톱 30일 고정, 손절폭 스윕) ---")
    for s in (0.10, 0.15, 0.20, 0.25, 0.30, 0.99):
        gross, net, hold, reason, R = simulate(P, ent, avail, s, 0, 0, 0, 30, 0)
        taken, meta = portfolio(sig, ent, avail, net, hold, reason, slots=SLOTS)
        lab = "손절없음" if s > 0.9 else f"손절{int(s*100)}%"
        print("   " + summarize(sig, net, hold, reason, taken, meta, SLOTS, lab))


if __name__ == "__main__":
    main()
