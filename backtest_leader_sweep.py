"""대장주 파이프라인 파라미터 스윕 — 데이터 1회 다운로드, 다수 조합 일괄 평가.

축:
  · 진입모드  : breakout(9~9:40 고점돌파) / pullback(전저점+1% 눌림)
  · 손절폭    : 2 / 3 / 4 %
  · 트레일폭  : 1.5 / 2 / 3 %
  · 사다리    : 균등(1·2·3·4%×20%) / 앞당김(1%40·2%30·3%20)
  · 시간청산  : 없음 / 12:00 / 13:00

선별은 고정(등락 >= +3% AND 9~9:40거래대금 >= 2×평소, 하루 상위 3).
'살아나는 조합'이 있는지 결정적으로 확인.

사용: python backtest_leader_sweep.py [period]
"""
from __future__ import annotations

import os, sys, tempfile, shutil

try:
    import certifi
    _dst = os.path.join(tempfile.gettempdir(), "cacert.pem")
    if not os.path.exists(_dst):
        shutil.copy(certifi.where(), _dst)
    os.environ.setdefault("CURL_CA_BUNDLE", _dst)
    os.environ.setdefault("SSL_CERT_FILE", _dst)
except Exception:
    pass

import itertools

import backtest_leader_swing as sw
import backtest_leader_pipeline as pl


def _run_config(store, all_days, leaders_per_day):
    """현재 sw.* 전역 설정으로 선별+매매 → trades 리스트."""
    trades = []
    for d in all_days:
        cands = []
        for tk, data in store.items():
            if d not in data["rate10"] or d not in data["val910"]:
                continue
            if data["rate10"][d] < pl.RISE_MIN:
                continue
            days_idx = {dd: i for i, dd in enumerate(data["days"])}
            avg = pl._avg_prev_val910(data, d, days_idx)
            if avg <= 0 or data["val910"][d] / avg < pl.VOL_MULT:
                continue
            cands.append((tk, data["rate10"][d]))
        if not cands:
            continue
        cands.sort(key=lambda x: -x[1])
        for tk, _ in cands[:leaders_per_day]:
            tr = sw._simulate_day(store[tk]["by_day"][d])
            if tr:
                trades.append(tr)
    return trades


LADDERS = {
    "균등": [(1.0, 0.20), (2.0, 0.20), (3.0, 0.20), (4.0, 0.20)],
    "앞당김": [(1.0, 0.40), (2.0, 0.30), (3.0, 0.20)],
}


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else "60d"
    leaders_per_day = 3

    print(f"파라미터 스윕 | 기간 {period} | 유니버스 {len(pl.UNIVERSE)}종목")
    print("데이터 다운로드 중…")
    store = {}
    for tk in pl.UNIVERSE:
        data = pl._prep(tk, period)
        if data:
            store[tk] = data
    all_days = sorted(set(d for v in store.values() for d in v["days"]))
    print(f"데이터 {len(store)}종목 / {len(all_days)}일\n")

    modes   = ["breakout", "pullback"]
    stops   = [2.0, 3.0, 4.0]
    trails  = [0.015, 0.02, 0.03]
    ladders = ["균등", "앞당김"]
    tstops  = [None, (12, 0), (13, 0)]

    W = 86
    print("=" * W)
    print(f"{'모드':<9}{'손절':>5}{'트레일':>6}{'사다리':>7}{'시간청산':>8}"
          f"{'거래':>5}{'승률':>6}{'평균':>7}{'누적':>8}{'MDD':>7}{'최악':>7}")
    print("-" * W)

    results = []
    for mode, stop, trail, lad, tstop in itertools.product(
            modes, stops, trails, ladders, tstops):
        sw.ENTRY_MODE = mode
        sw.STOP_PCT = stop
        sw.TRAIL_GIVEBACK = trail
        sw.LADDER = LADDERS[lad]
        sw.FULL_OUT_PCT = 5.0
        sw.TIME_STOP = tstop
        trades = _run_config(store, all_days, leaders_per_day)
        s = sw._stats(trades)
        if s["n"] == 0:
            continue
        ts_lbl = "-" if tstop is None else f"{tstop[0]:02d}:{tstop[1]:02d}"
        row = (mode, stop, trail, lad, ts_lbl, s)
        results.append(row)

    # 누적수익 순 정렬, 상위/하위 출력
    results.sort(key=lambda r: -r[5]["total"])
    def _pr(r):
        mode, stop, trail, lad, ts_lbl, s = r
        print(f"{mode:<9}{stop:>4.0f}%{trail*100:>5.1f}%{lad:>7}{ts_lbl:>8}"
              f"{s['n']:>5}{s['wr']:>5.0f}%{s['avg']:>+6.2f}%{s['total']:>+7.1f}%"
              f"{s['mdd']:>6.1f}%{s['worst']:>+6.1f}%")
    print("  ── 상위 10 (누적순) ──")
    for r in results[:10]:
        _pr(r)
    print("  ── 하위 5 ──")
    for r in results[-5:]:
        _pr(r)
    print("=" * W)
    pos = [r for r in results if r[5]["total"] > 0]
    print(f"플러스 누적 조합: {len(pos)}/{len(results)}")
    if pos:
        best = results[0][5]
        print(f"최고 조합 누적 {best['total']:+.1f}% / 승률 {best['wr']:.0f}% / "
              f"거래 {best['n']} / MDD {best['mdd']:.1f}%")
        print("→ 플러스 조합이 표본·기간에 강건한지(다른 기간/종목) 추가 검증 필요")
    else:
        print("→ 어떤 조합도 플러스 아님: 이 접근(급등 대장주 5분봉 당일매매)은")
        print("  현 표본·기간에서 엣지가 없음. 전략 컨셉 재고 권장.")


if __name__ == "__main__":
    main()
