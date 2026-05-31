"""대장주 5분봉 — 롤링 30분 돌파 진입 백테스트.

backtest_leader_swing.py 와 청산 룰(손절·사다리·트레일·마감)은 동일하되,
진입만 "고정 관찰창(09:00~09:40)" → "롤링 30분 재기준 돌파"로 교체한다.

진입 룰 (사용자 설계):
  · 09:00 부터 30분 블록으로 구간을 나눈다: [9:00,9:30),[9:30,10:00),...
  · 매 30분 체크포인트(9:30,10:00,10:30...)마다 '직전 30분 구간 고가'를 돌파기준으로
    새로 설정한다(재기준).
  · 보유 전(미진입)이면, 현재 봉이 직전 블록 고가를 넘으면 그 가격에 전량 매수.
  · 하루 1회만 진입. 진입 후엔 backtest_leader_swing 과 동일한 청산 룰 적용.

사용:
  python backtest_leader_rolling.py [symbols] [period]
  예) python backtest_leader_rolling.py 064400 60d
"""
from __future__ import annotations

import sys
from collections import Counter

import pandas as pd

import backtest_leader_swing as B  # _download, 상수, _stats 재사용

BLOCK_MIN = 30          # 재기준 주기(분)
START_HM  = (9, 0)      # 블록 기준 시작 시각


def _mins_since_start(ts) -> int:
    return (ts.hour - START_HM[0]) * 60 + (ts.minute - START_HM[1])


def _simulate_day_rolling(day: pd.DataFrame) -> dict | None:
    """하루 롤링-돌파 시뮬. 진입했으면 트레이드 dict, 미진입이면 None."""
    # 1) 30분 블록별 고가 사전계산 (블록 bi-1 은 블록 bi 진입 전 항상 완성 → 룩어헤드 없음)
    block_high: dict[int, float] = {}
    for ts, r in day.iterrows():
        bi = _mins_since_start(ts) // BLOCK_MIN
        block_high[bi] = max(block_high.get(bi, -1.0), float(r["high"]))

    in_pos = False
    entry = 0.0
    stop_price = 0.0
    ref_used = 0.0
    qty = 0.0
    peak = 0.0
    hit: set = set()
    exits: list = []

    for ts, r in day.iterrows():
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        bi = _mins_since_start(ts) // BLOCK_MIN

        if not in_pos:
            if bi < 1:
                continue  # 09:30 이전: 직전 완성 블록 없음 → 관찰만
            trigger = block_high.get(bi - 1)   # 직전 30분 구간 고가(재기준)
            if trigger is None or trigger <= 0:
                continue
            if h >= trigger:                   # 돌파 → 진입(갭상승이면 시가)
                fill = max(o, trigger) if o > trigger else trigger
                in_pos = True
                entry = fill
                ref_used = trigger
                stop_price = entry * (1 - B.STOP_PCT / 100)
                qty = 1.0
                peak = fill
            continue

        # 보유 중 — 청산 룰은 backtest_leader_swing 과 동일
        peak = max(peak, h)
        peak_pct = (peak / entry - 1) * 100

        if B.TIME_STOP is not None and (ts.hour, ts.minute) >= B.TIME_STOP:
            exits.append((qty, c, f"시간청산({B.TIME_STOP[0]:02d}:{B.TIME_STOP[1]:02d})"))
            qty = 0.0
            break

        if l <= stop_price:                                  # -3% 손절
            px = min(o, stop_price) if o < stop_price else stop_price
            exits.append((qty, px, f"손절(-{B.STOP_PCT:g}%)"))
            qty = 0.0
            break

        if peak_pct >= B.TRAIL_ARM_PCT:                      # 트레일링
            trail_px = peak * (1 - B.TRAIL_GIVEBACK)
            if l <= trail_px:
                px = min(o, trail_px) if o < trail_px else trail_px
                exits.append((qty, px, f"트레일링(고점-{B.TRAIL_GIVEBACK*100:.0f}%)"))
                qty = 0.0
                break

        for lvl, frac in B.LADDER:                           # 익절 사다리
            if lvl in hit:
                continue
            target = entry * (1 + lvl / 100)
            if h >= target:
                exits.append((min(frac, qty), target, f"+{lvl:g}%"))
                qty -= frac
                hit.add(lvl)
        if qty > 0 and h >= entry * (1 + B.FULL_OUT_PCT / 100):
            exits.append((qty, entry * (1 + B.FULL_OUT_PCT / 100), f"+{B.FULL_OUT_PCT:g}% 전량"))
            qty = 0.0
            break

    if not in_pos:
        return None

    if qty > 1e-9:                                           # 마감청산
        last_c = float(day["close"].iloc[-1])
        exits.append((qty, last_c, "마감청산"))
        qty = 0.0

    proceeds = sum(f * px * (1 - B.SELL_COMM) for f, px, _ in exits)
    cost = entry * (1 + B.BUY_COMM)
    pnl_pct = (proceeds / cost - 1) * 100
    return {
        "date": str(day.index[0].date()),
        "entry": entry, "ref": ref_used, "stop": stop_price,
        "pnl_pct": pnl_pct, "exits": exits,
        "reasons": [r for _, _, r in exits],
    }


def _backtest(df: pd.DataFrame) -> list[dict]:
    trades = []
    for d in sorted(set(df.index.date)):
        day = df[df.index.date == d]
        tr = _simulate_day_rolling(day)
        if tr:
            trades.append(tr)
    return trades


def main():
    sym_arg = sys.argv[1] if len(sys.argv) > 1 else "005930,000660,035420,000270,005380"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in sym_arg.split(",")]

    print(f"대장주 5분봉 롤링30분돌파 백테스트 | 종목 {symbols} | 기간 {period}")
    print(f"진입 매30분 직전구간고가 돌파(재기준) / 손절 진입가-{B.STOP_PCT:g}% / "
          f"사다리 +1·2·3·4%×20% +5%전량 / 트레일 고점-{B.TRAIL_GIVEBACK*100:g}%"
          f"(arm +{B.TRAIL_ARM_PCT:g}%)")
    W = 90
    all_trades = []
    print("=" * W)
    print(f"{'종목':<10} {'거래':>4} {'진입일수':>7} {'승률':>6} {'평균':>7} "
          f"{'평균익':>7} {'평균손':>7} {'누적':>8} {'MDD':>7} {'최고':>7} {'최악':>7}")
    print("-" * W)
    for sym in symbols:
        try:
            df = B._download(sym, period)
        except Exception as e:
            print(f"{sym:<10} 다운로드 실패 — {e}")
            continue
        days = len(set(df.index.date))
        trades = _backtest(df)
        all_trades += trades
        s = B._stats(trades)
        if s["n"] == 0:
            print(f"{sym:<10} {0:>4} {days:>7} {'-':>6} (진입 없음)")
            continue
        print(f"{sym:<10} {s['n']:>4} {days:>7} {s['wr']:>5.0f}% {s['avg']:>+6.2f}% "
              f"{s['avg_win']:>+6.2f}% {s['avg_loss']:>+6.2f}% {s['total']:>+7.2f}% "
              f"{s['mdd']:>6.1f}% {s['best']:>+6.2f}% {s['worst']:>+6.2f}%")
    print("-" * W)
    g = B._stats(all_trades)
    if g["n"]:
        print(f"{'합계':<10} {g['n']:>4} {'':>7} {g['wr']:>5.0f}% {g['avg']:>+6.2f}% "
              f"{g['avg_win']:>+6.2f}% {g['avg_loss']:>+6.2f}% {g['total']:>+7.2f}% "
              f"{g['mdd']:>6.1f}% {g['best']:>+6.2f}% {g['worst']:>+6.2f}%")
    rc = Counter(r for t in all_trades for r in t["reasons"])
    print("\n청산 사유 분포:", dict(rc))
    print("=" * W)


if __name__ == "__main__":
    main()
