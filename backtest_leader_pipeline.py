"""대장주 선별 + 5분봉 스윙 매매룰 = 전체 파이프라인 백테스트.

backtest_leader_swing.py 는 매매룰만 무작위 대형주에 적용 → 선별 엣지가 없어
결과가 무의미했음. 이 스크립트는 과거 각 날짜의 '대장주'를 근사 복원해 선별 엣지를
주입한 뒤 매매룰을 적용한다.

대장주 근사 복원 (과거 5분봉으로 그날 10:00 시점 재구성):
  · 등락률@10시  = (10:00 종가 / 전일종가 - 1)
  · 9~10시 거래대금 = Σ(거래량 × 전형가)  (전형가=(고+저+종)/3)
  · 평소 9~10시 거래대금 = 직전 최대 5거래일의 같은 시간대 거래대금 평균
  · 선별 조건: 등락률 >= RISE_MIN  AND  9~10거래대금 >= VOL_MULT × 평소
  · 그날 조건 통과 종목을 등락률순으로 상위 LEADERS_PER_DAY 채택
매매룰: backtest_leader_swing._simulate_day (진입 전저점+1%/손절 -3%/사다리/트레일)

데이터: yfinance 5분봉(약 60일). 유니버스는 대표 유동/테마주 표본(아래 UNIVERSE).
       ※ 실제 leader_finder 유니버스(코스피+코스닥 전체)의 부분집합 — 표본 한계 있음.

사용: python backtest_leader_pipeline.py [period] [leaders_per_day]
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

import pandas as pd
from collections import Counter

import backtest_leader_swing as sw   # 매매룰·통계 재사용

# ── 선별 파라미터 ────────────────────────────────────────────────────
RISE_MIN        = 3.0     # 10시 등락률 하한 %
VOL_MULT        = 2.0     # 9~10시 거래대금 / 평소 배수 게이트
LEADERS_PER_DAY = 3       # 하루 채택 대장주 수
TEN = (10, 0)
NINE = (9, 0)

# 대표 유동/테마주 표본 (코스피 .KS / 코스닥 .KQ)
UNIVERSE = [
    # 대형 코스피
    "005930.KS","000660.KS","035420.KS","035720.KS","005380.KS","000270.KS",
    "005490.KS","051910.KS","006400.KS","207940.KS","068270.KS","012330.KS",
    "066570.KS","003670.KS","373220.KS","028260.KS","105560.KS","015760.KS",
    "032830.KS","096770.KS","034730.KS","009150.KS","011200.KS","010130.KS",
    # 코스닥 테마(2차전지·바이오·반도체 소부장·게임·엔터)
    "247540.KQ","086520.KQ","091990.KQ","028300.KQ","196170.KQ","293490.KQ",
    "263750.KQ","041510.KQ","357780.KQ","058470.KQ","240810.KQ","022100.KQ",
    "095340.KQ","005290.KQ","039030.KQ","403870.KQ",
]


def _typ_value(m: pd.DataFrame) -> float:
    tp = (m["high"] + m["low"] + m["close"]) / 3
    return float((m["volume"].astype(float) * tp).sum())


def _prep(ticker: str, period: str):
    """ticker 5분봉 → (date -> day_df), prev_close map, val910 map. 실패 시 None."""
    import yfinance as yf
    try:
        df = yf.download(ticker, period=period, interval="5m",
                         auto_adjust=True, progress=False, timeout=40)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Seoul")

    days = sorted(set(df.index.date))
    by_day, prev_close, val910, rate10 = {}, {}, {}, {}
    for i, d in enumerate(days):
        day = df[df.index.date == d]
        by_day[d] = day
        if i > 0:
            pc = float(df[df.index.date == days[i - 1]]["close"].iloc[-1])
            prev_close[d] = pc
        m = day[[(NINE <= (ts.hour, ts.minute) < TEN) for ts in day.index]]
        if len(m) >= 1:
            val910[d] = _typ_value(m)
        ten = day[[(ts.hour, ts.minute) <= TEN for ts in day.index]]
        if len(ten) >= 1 and d in prev_close and prev_close[d] > 0:
            rate10[d] = (float(ten["close"].iloc[-1]) / prev_close[d] - 1) * 100
    return {"days": days, "by_day": by_day, "prev_close": prev_close,
            "val910": val910, "rate10": rate10}


def _avg_prev_val910(data: dict, d, days_idx: dict) -> float:
    """직전 최대 5거래일의 9~10시 거래대금 평균."""
    i = days_idx[d]
    prev = []
    for j in range(i - 1, -1, -1):
        dd = data["days"][j]
        if dd in data["val910"] and data["val910"][dd] > 0:
            prev.append(data["val910"][dd])
        if len(prev) >= 5:
            break
    return sum(prev) / len(prev) if prev else 0.0


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else "60d"
    leaders_per_day = int(sys.argv[2]) if len(sys.argv) > 2 else LEADERS_PER_DAY

    print(f"대장주 선별+매매 파이프라인 백테스트 | 기간 {period} | 유니버스 {len(UNIVERSE)}종목")
    print(f"선별: 10시등락 >= +{RISE_MIN:g}% AND 9~10거래대금 >= {VOL_MULT:g}×평소 | "
          f"하루 상위 {leaders_per_day}종목")
    print(f"매매: 진입 전저점+{sw.ENTRY_ABOVE*100:g}% / 손절 -{sw.STOP_PCT:g}% / "
          f"사다리 +1·2·3·4%×20% +5%전량 / 트레일 고점-{sw.TRAIL_GIVEBACK*100:g}%")
    print("=" * 70)

    # 1) 전 종목 데이터 준비
    store = {}
    for tk in UNIVERSE:
        data = _prep(tk, period)
        if data:
            store[tk] = data
    print(f"데이터 확보: {len(store)}/{len(UNIVERSE)}종목")
    if not store:
        print("데이터 없음 — 종료"); return

    # 공통 날짜 집합
    all_days = sorted(set(d for v in store.values() for d in v["days"]))

    # 2) 날짜별 선별 → 매매
    trades, picks_log = [], []
    n_candidate_days = 0
    for d in all_days:
        cands = []
        for tk, data in store.items():
            if d not in data["rate10"] or d not in data["val910"]:
                continue
            rate = data["rate10"][d]
            if rate < RISE_MIN:
                continue
            days_idx = {dd: i for i, dd in enumerate(data["days"])}
            avg = _avg_prev_val910(data, d, days_idx)
            if avg <= 0:
                continue
            ratio = data["val910"][d] / avg
            if ratio < VOL_MULT:
                continue
            cands.append((tk, rate, ratio))
        if not cands:
            continue
        n_candidate_days += 1
        cands.sort(key=lambda x: -x[1])
        chosen = cands[:leaders_per_day]
        for tk, rate, ratio in chosen:
            day = store[tk]["by_day"][d]
            tr = sw._simulate_day(day)
            if tr:
                tr["ticker"] = tk; tr["rate10"] = rate; tr["volx"] = ratio
                trades.append(tr)
                picks_log.append((str(d), tk, rate, ratio, tr["pnl_pct"]))

    # 3) 결과
    s = sw._stats(trades)
    print(f"\n선별된 날: {n_candidate_days}일 (전체 {len(all_days)}일 중) | "
          f"진입 트레이드: {s['n']}건")
    if s["n"] == 0:
        print("진입 없음 — 조건이 너무 빡빡하거나 표본 부족"); return
    print("-" * 70)
    print(f"승률      {s['wr']:.0f}%")
    print(f"평균손익  {s['avg']:+.2f}%   (평균익 {s['avg_win']:+.2f}% / 평균손 {s['avg_loss']:+.2f}%)")
    print(f"누적수익  {s['total']:+.2f}%   MDD {s['mdd']:.1f}%")
    print(f"최고/최악 {s['best']:+.2f}% / {s['worst']:+.2f}%")
    rc = Counter(r for t in trades for r in t["reasons"])
    print(f"\n청산 사유 분포: {dict(rc)}")

    # 종목별 선별 빈도 / 손익
    by_tk = {}
    for t in trades:
        by_tk.setdefault(t["ticker"], []).append(t["pnl_pct"])
    print("\n자주 선별된 종목 (건수·평균손익):")
    for tk, ps in sorted(by_tk.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"  {tk:<11} {len(ps):>3}건  평균 {sum(ps)/len(ps):+.2f}%")

    print("\n최근 선별 예시 (날짜·종목·10시등락·거래대금배수·트레이드손익):")
    for row in picks_log[-12:]:
        print(f"  {row[0]} {row[1]:<11} 등락{row[2]:+.1f}% {row[3]:.1f}x  → {row[4]:+.2f}%")
    print("=" * 70)


if __name__ == "__main__":
    main()
