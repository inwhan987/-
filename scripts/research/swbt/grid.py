# -*- coding: utf-8 -*-
"""전략 1개 상세 점검: 게이트 퍼널 → 진입 모델 → MFE/MAE → 청산 그리드.

사용: python grid.py NEWHIGH [--entry trigger|open] [--nogate]
"""
from __future__ import annotations
import sys, os, pickle, itertools, argparse
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# 신호 패널(.pkl, 4GB)은 git 에 못 넣는다 → 저장소 옆 고정 경로. 코드만 저장소에 있다.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
DATA = os.environ.get("SWBT", os.path.join(os.path.dirname(_REPO), "swbt"))
KIND = {"BREAKOUT": "breakout", "NEWHIGH": "breakout", "GAPGO": "breakout", "VALUE_MOM": "breakout",
        "PULLBACK": "pullback", "FLOW_PULLBACK": "hold", "MEANREV": "pullback",  # 라이브 daily_scan.TRIGGER_KIND 와 일치 (2026-09-22 변경)
        "FLOW_FORGN": "hold", "FLOW_INST": "hold", "FLOW_BOTH": "hold", "VALUE_PURE": "hold", "MOMENTUM": "hold"}
BUY_COST = 0.00015 + 0.0010
SELL_COST = 0.00015 + 0.0015 + 0.0010
# 라이브 게이트 (.env.overrides 현재값)
G = dict(min_score=60, min_value_eok=30, min_cap_eok=1000, min_price=1000, max_atr=0.07)


def load():
    with open(os.path.join(DATA, "signals.pkl"), "rb") as f:
        return pickle.load(f)


def funnel(sig: pd.DataFrame, gate=True):
    steps = [("전체 신호", np.ones(len(sig), bool))]
    m = sig["avail"].to_numpy() >= 1
    steps.append(("다음날 데이터 있음", m))
    if gate:
        for lab, cond in [("종합점수≥60", sig["score"] >= G["min_score"]),
                          ("거래대금 20일평균≥30억", sig["value_eok"] >= G["min_value_eok"]),
                          ("시총≥1000억", sig["mktcap_eok"] >= G["min_cap_eok"]),
                          ("주가≥1000원", sig["close"] >= G["min_price"]),
                          ("ATR%≤7%", sig["atr_pct"] <= G["max_atr"]),
                          ("레짐 OK (KOSPI>200일선)", sig["regime"] >= 1)]:
            m = m & cond.fillna(False).to_numpy()
            steps.append((lab, m.copy()))
    return steps, m


def entry_model(sig: pd.DataFrame, P: np.ndarray, kind: str, mode: str):
    """진입가 배열(NaN=미진입). day0 = 신호 다음날."""
    o, h, l, c = P[:, 0, 0], P[:, 0, 1], P[:, 0, 2], P[:, 0, 3]
    pc = sig["close"].to_numpy()
    if mode == "open":
        return o.copy(), np.full(len(sig), "시가", dtype=object)
    ent = np.full(len(sig), np.nan)
    why = np.full(len(sig), "", dtype=object)
    if kind == "breakout":
        top = sig["box_top"].to_numpy()
        ok = h > top
        ent[ok] = np.maximum(o, top)[ok]
        why[~ok] = "박스미돌파"
    elif kind == "pullback":
        ma = sig["ma20"].to_numpy()
        touch = l <= ma * 1.02
        bounce = h > pc * 1.002   # 룩어헤드 제거: D+1 종가 조건 삭제
        ok = touch & bounce
        ent[ok] = np.clip(pc * 1.002, l, h)[ok]
        why[~touch] = "미눌림"; why[touch & ~bounce] = "반등없음"
    else:  # hold
        ok = h > pc * 1.002
        ent[ok] = np.maximum(o, pc * 1.002)[ok]
        why[~ok] = "전일종가하회"
    why[ok] = "진입"
    return ent, why


def simulate(P, ent, avail, stop, tp, tr_after, tr_pct, tdays, ma_exit):
    """모든 거래를 날짜축으로 벡터 시뮬. 반환 exit_ret(gross), hold_days, reason(int)."""
    n, H, _ = P.shape
    alive = np.isfinite(ent)
    exit_px = np.full(n, np.nan); hold = np.zeros(n, int); reason = np.full(n, 0, int)   # 0=미종결
    stop_px = ent * (1 - stop)
    tp_px = ent * (1 + tp) if tp > 0 else np.full(n, np.inf)
    peak = ent.copy(); trail_on = np.zeros(n, bool)
    R = {1: "손절", 2: "익절", 3: "트레일링", 4: "타임스톱", 5: "이평이탈", 9: "데이터끝"}
    for d in range(H):
        o, h, l, c, ma = P[:, d, 0], P[:, d, 1], P[:, d, 2], P[:, d, 3], P[:, d, 4]
        cur = alive & (d < avail)
        if not cur.any():
            break
        px = np.full(n, np.nan); rs = np.zeros(n, int)
        # 손절 (갭 → 시가, 아니면 손절가). 진입 당일은 갭 판정 없음(진입가 ≥ 시가일 수 있음)
        m = cur & (d > 0) & (o <= stop_px); px[m] = o[m]; rs[m] = 1
        m = cur & (rs == 0) & (l <= stop_px); px[m] = stop_px[m]; rs[m] = 1
        if tp > 0:
            m = cur & (rs == 0) & (d > 0) & (o >= tp_px); px[m] = o[m]; rs[m] = 2
            m = cur & (rs == 0) & (h >= tp_px); px[m] = tp_px[m]; rs[m] = 2
        if tr_pct > 0:
            peak = np.where(cur, np.fmax(peak, h), peak)
            trail_on = trail_on | (cur & (h >= ent * (1 + tr_after)))
            m = cur & (rs == 0) & trail_on & (c <= peak * (1 - tr_pct)); px[m] = c[m]; rs[m] = 3
        if tdays > 0:
            m = cur & (rs == 0) & (d + 1 >= tdays); px[m] = c[m]; rs[m] = 4
        if ma_exit:
            m = cur & (rs == 0) & np.isfinite(ma) & (c < ma); px[m] = c[m]; rs[m] = 5
        # 데이터 끝
        m = cur & (rs == 0) & (d + 1 >= avail); px[m] = c[m]; rs[m] = 9
        done = cur & (rs > 0)
        exit_px[done] = px[done]; hold[done] = d + 1; reason[done] = rs[done]
        alive = alive & ~done
    gross = exit_px / ent - 1
    net = (exit_px * (1 - SELL_COST)) / (ent * (1 + BUY_COST)) - 1
    return gross, net, hold, reason, R


def stats(net, hold, reason, R, label):
    m = np.isfinite(net) & (reason != 9)
    x = net[m]
    if len(x) < 5:
        return dict(label=label, n=int(len(x)))
    w = x[x > 0]; lo = x[x <= 0]
    pf = w.sum() / -lo.sum() if lo.sum() < 0 else np.inf
    t = x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 1 else np.nan
    rmix = {R[k]: int((reason[m] == k).sum()) for k in sorted(set(reason[m]))}
    return dict(label=label, n=int(len(x)), win=float((x > 0).mean()), mean=float(x.mean()), med=float(np.median(x)),
                pf=float(pf), t=float(t), hold=float(hold[m].mean()), avgwin=float(w.mean()) if len(w) else 0,
                avgloss=float(lo.mean()) if len(lo) else 0, p10=float(np.percentile(x, 10)), p90=float(np.percentile(x, 90)),
                sum=float(x.sum()), reasons=rmix, unfinished=int((reason == 9).sum()))


def fmt(s):
    if "win" not in s:
        return f"{s['label']:<34} n={s['n']:<5} (표본 부족)"
    rm = " ".join(f"{k}{v}" for k, v in s["reasons"].items())
    return (f"{s['label']:<34} n={s['n']:<5} 승률 {s['win']*100:5.1f}%  평균 {s['mean']*100:+6.2f}%  중앙 {s['med']*100:+6.2f}%"
            f"  PF {s['pf']:4.2f}  t {s['t']:+5.1f}  보유 {s['hold']:4.1f}일  평균익 {s['avgwin']*100:+5.1f}% 평균손 {s['avgloss']*100:+5.1f}%  [{rm}]")


def mfe_mae(P, ent, avail, out):
    m = np.isfinite(ent)
    Pm, e, av = P[m], ent[m], avail[m]
    out.append("\n── MFE/MAE (진입가 기준, 청산 규칙 없이 그냥 들고 있었을 때) ──")
    for hz in (5, 10, 20, 30):
        ok = av >= hz
        if ok.sum() < 20:
            continue
        hh = Pm[ok, :hz, 1]; ll = Pm[ok, :hz, 2]; cc = Pm[ok, hz - 1, 3]
        mfe = np.nanmax(hh, 1) / e[ok] - 1; mae = np.nanmin(ll, 1) / e[ok] - 1; fin = cc / e[ok] - 1
        q = lambda a, p: np.nanpercentile(a, p) * 100
        out.append(f"[{hz:>2}일] n={ok.sum():<5} 최종수익 평균 {np.nanmean(fin)*100:+5.2f}% 중앙 {q(fin,50):+5.2f}% 승률 {(fin>0).mean()*100:4.1f}%"
                   f" | 최대상승(MFE) 중앙 {q(mfe,50):+5.1f}% 상위25% {q(mfe,75):+5.1f}% 상위10% {q(mfe,90):+5.1f}%"
                   f" | 최대하락(MAE) 중앙 {q(mae,50):+5.1f}% 하위25% {q(mae,25):+5.1f}% 하위10% {q(mae,10):+5.1f}%")
        if hz == 20:
            out.append("   20일 뒤 +5% 이상 이긴 거래 중, 중간에 손절선 X 를 찍었던 비율(손절이 너무 타이트하면 이긴 거래를 죽인다):")
            winners = fin > 0.05
            line = "   "
            for s in (0.03, 0.05, 0.07, 0.10, 0.15, 0.20):
                line += f" 손절{int(s*100)}%: {(mae[winners] <= -s).mean()*100:4.1f}%  "
            out.append(line + f"(이긴 거래 n={winners.sum()})")
            out.append("   20일 뒤 -5% 이상 진 거래 중, 중간에 익절선 Y 를 찍었던 비율(익절이 있었으면 살렸을 거래):")
            losers = fin < -0.05
            line = "   "
            for tpv in (0.03, 0.05, 0.08, 0.10, 0.12, 0.15):
                line += f" 익절{int(tpv*100)}%: {(mfe[losers] >= tpv).mean()*100:4.1f}%  "
            out.append(line + f"(진 거래 n={losers.sum()})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy"); ap.add_argument("--entry", default="trigger"); ap.add_argument("--nogate", action="store_true")
    ap.add_argument("--from", dest="dfrom", default="20240101")
    a = ap.parse_args()
    D = load(); sig, P = D["sig"], D["paths"]
    st = a.strategy.upper()
    msk = (sig["strategy"] == st).to_numpy() & (sig["date"] >= pd.Timestamp(a.dfrom)).to_numpy()
    sig = sig[msk].reset_index(drop=True); P = P[msk]
    out = [f"===== {st} ({KIND.get(st)} 트리거) · 신호 {a.dfrom}~ · 진입모델 {a.entry} · 비용 매수 {BUY_COST*100:.3f}% 매도 {SELL_COST*100:.3f}% ====="]
    steps, m = funnel(sig, gate=not a.nogate)
    out.append("── 퍼널 (라이브 진입 게이트 순서대로) ──")
    for lab, mm in steps:
        out.append(f"  {lab:<26} {mm.sum():>7}")
    sig = sig[m].reset_index(drop=True); P = P[m]
    ent, why = entry_model(sig, P, KIND.get(st, "hold"), a.entry)
    vc = pd.Series(why).value_counts()
    out.append("── 다음날 장중 트리거(일봉 근사) ──  " + "  ".join(f"{k} {v}" for k, v in vc.items()))
    avail = sig["avail"].to_numpy()
    ok = np.isfinite(ent)
    out.append(f"  진입 {ok.sum()}건 · 연도별: " + "  ".join(f"{y} {n}" for y, n in sig.loc[ok, "date"].dt.year.value_counts().sort_index().items()))
    slip = (ent[ok] / sig["close"].to_numpy()[ok] - 1)
    out.append(f"  진입가/신호일종가: 평균 {slip.mean()*100:+.2f}% 중앙 {np.median(slip)*100:+.2f}% (트리거 대기 비용)")
    mfe_mae(P, ent, avail, out)

    out.append("\n── 청산 그리드 (현재 운용값 = 손절20% · 익절없음 · 트레일없음 · 타임스톱30일 · 이평없음) ──")
    base = stats(*simulate(P, ent, avail, 0.20, 0, 0, 0, 30, 0)[1:], "★현재 stop20/tp0/trail0/t30/ma0")
    out.append(fmt(base))
    out.append("\n[1] 손절만 바꿔보기 (나머지 현재값)")
    for s in (0.03, 0.05, 0.07, 0.10, 0.15, 0.20, 0.30):
        out.append(fmt(stats(*simulate(P, ent, avail, s, 0, 0, 0, 30, 0)[1:], f"stop{int(s*100)}")))
    out.append("\n[2] 익절 추가 (손절 20%, 타임스톱 30)")
    for tp in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.30):
        out.append(fmt(stats(*simulate(P, ent, avail, 0.20, tp, 0, 0, 30, 0)[1:], f"tp{int(tp*100)}")))
    out.append("\n[3] 트레일링 추가 (손절 20%, 익절 없음, 타임스톱 30) — 발동>폭")
    for ta, tp_ in ((0.03, 0.03), (0.05, 0.03), (0.05, 0.05), (0.08, 0.05), (0.10, 0.05), (0.08, 0.08), (0.12, 0.08)):
        out.append(fmt(stats(*simulate(P, ent, avail, 0.20, 0, ta, tp_, 30, 0)[1:], f"trail{int(ta*100)}>{int(tp_*100)}")))
    out.append("\n[4] 타임스톱만 (손절 20%)")
    for td in (3, 5, 10, 15, 20, 30, 45):
        out.append(fmt(stats(*simulate(P, ent, avail, 0.20, 0, 0, 0, td, 0)[1:], f"time{td}")))
    out.append("\n[5] 20일선 이탈 청산 켜기 (손절 20%, 타임스톱 30)")
    out.append(fmt(stats(*simulate(P, ent, avail, 0.20, 0, 0, 0, 30, 20)[1:], "ma20 on")))
    out.append(fmt(stats(*simulate(P, ent, avail, 0.10, 0, 0, 0, 30, 20)[1:], "stop10 + ma20")))
    out.append("\n[6] 조합 상위 (손절×익절×트레일×타임 전수 — 평균수익 t값 기준 정렬, 상위 15 / 하위 5)")
    combos = []
    for s, tp, (ta, tw), td, ma in itertools.product((0.05, 0.07, 0.10, 0.15, 0.20), (0, 0.08, 0.12, 0.20),
                                                    ((0, 0), (0.05, 0.03), (0.08, 0.05), (0.12, 0.08)), (10, 20, 30), (0, 20)):
        r = stats(*simulate(P, ent, avail, s, tp, ta, tw, td, ma)[1:], f"stop{int(s*100)}/tp{int(tp*100)}/trail{int(ta*100)}>{int(tw*100)}/t{td}/ma{ma}")
        if "win" in r:
            combos.append(r)
    combos.sort(key=lambda r: -r["t"])
    pos = sum(1 for r in combos if r["mean"] > 0)
    out.append(f"  전체 {len(combos)} 조합 중 평균수익 플러스 {pos}개 ({pos/len(combos)*100:.0f}%)")
    for r in combos[:15]:
        out.append(fmt(r))
    out.append("  ...")
    for r in combos[-5:]:
        out.append(fmt(r))
    out.append("\n[7] 연도별 안정성 (현재 규칙 vs 조합 1위)")
    best = combos[0]["label"]
    bp = best.replace("stop", "").replace("tp", "").replace("trail", "").replace("t", "").replace("ma", "").split("/")
    bs, btp, btr, btd, bma = float(bp[0]) / 100, float(bp[1]) / 100, bp[2], int(bp[3]), int(bp[4])
    bta, btw = (float(x) / 100 for x in btr.split(">"))
    years = sig["date"].dt.year.to_numpy()
    for y in sorted(set(years)):
        ym = years == y
        for lab, args in (("현재", (0.20, 0, 0, 0, 30, 0)), ("1위", (bs, btp, bta, btw, btd, bma))):
            r = stats(*simulate(P[ym], ent[ym], avail[ym], *args)[1:], f"{y} {lab}")
            out.append(fmt(r))
    txt = "\n".join(out)
    print(txt)
    with open(os.path.join(DATA, f"result_{st}_{a.entry}.txt"), "w", encoding="utf-8") as f:
        f.write(txt)


if __name__ == "__main__":
    main()
