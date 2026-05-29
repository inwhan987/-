"""대장주 5분봉 스윙 전략 백테스트 (leader_finder 로 선별된 대장주에 적용할 매매룰 검증).

전략 (사용자 설계):
  · 대상      : 10:00 에 선별된 대장주 (여기선 선별 대신 주어진 종목으로 룰만 검증)
  · 차트      : 09:00~10:00 5분봉
  · 전 저점   : 09~10시 구간의 '직전 스윙 저점'(좌우 swing_window 봉보다 낮은 골 중 가장 최근)
  · 진입      : 전저점 +1% 위 지점에서 선진입(전량). entry = swing_low * (1 + ENTRY_ABOVE)
                10:00 이후 가격이 entry 까지 눌리면 그 가격에 전량 매수.
  · 손절      : 전저점 이탈(저가 <= swing_low) 즉시 전량 손절
  · 익절 사다리: +1% 20%, +2% 20%, +3% 20%, +4% 20%, +5% 남은 전량
  · 트레일링  : 도달 고점 대비 -2% 하락 시 남은 전량 (peak_pct >= ARM_PCT 일 때만 작동)
  · 장 마감   : 남은 물량 종가 청산(당일 청산, 오버나잇 없음)

사용:
  python backtest_leader_swing.py [symbols] [period]
  예) python backtest_leader_swing.py 005930,000660,035420 60d
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

# ── 전략 파라미터 ────────────────────────────────────────────────────
ENTRY_ABOVE   = 0.01          # 전저점 +1% 위 선진입
STOP_PCT      = 3.0           # 진입가 대비 -3% 고정 손절 (전저점 이탈 대신)
SWING_WINDOW  = 2             # 스윙 저점 좌우 비교 봉수
LADDER        = [(1.0, 0.20), (2.0, 0.20), (3.0, 0.20), (4.0, 0.20)]  # (수익%, 매도비중)
FULL_OUT_PCT  = 5.0           # +5% 남은 전량
TRAIL_GIVEBACK = 0.02         # 고점 대비 -2%
TRAIL_ARM_PCT  = 1.0          # 고점 수익률이 +1% 이상일 때만 트레일링 작동
MORNING_START = (9, 0)
MORNING_END   = (10, 0)
SESSION_END   = (15, 30)

BUY_COMM  = 0.00015
SELL_COMM = 0.00195


def _download(symbol, period):
    import yfinance as yf
    ticker = symbol if "." in symbol else f"{symbol}.KS"
    df = yf.download(ticker, period=period, interval="5m",
                     auto_adjust=True, progress=False, timeout=30)
    if df.empty:
        raise ValueError(f"no data {ticker}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Seoul")
    return df


def _swing_low(morning: pd.DataFrame, w: int) -> float | None:
    """직전 스윙 저점: 좌우 w봉보다 낮은 골 중 가장 최근. 없으면 구간 최저."""
    lows = morning["low"].values
    n = len(lows)
    if n == 0:
        return None
    swings = []
    for i in range(w, n - w):
        if (all(lows[i] <= lows[i - j] for j in range(1, w + 1)) and
                all(lows[i] <= lows[i + j] for j in range(1, w + 1))):
            swings.append(float(lows[i]))
    if swings:
        return swings[-1]          # 가장 최근 스윙 저점
    return float(lows.min())       # 폴백: 구간 최저


def _simulate_day(day: pd.DataFrame) -> dict | None:
    """하루 시뮬. 진입했으면 트레이드 dict, 미진입이면 None."""
    t = day.index
    morn_mask = [(MORNING_START <= (ts.hour, ts.minute) < MORNING_END) for ts in t]
    morning = day[morn_mask]
    if len(morning) < SWING_WINDOW * 2 + 1:
        return None
    swing = _swing_low(morning, SWING_WINDOW)
    if swing is None or swing <= 0:
        return None

    entry_trigger = swing * (1 + ENTRY_ABOVE)

    after = day[[(ts.hour, ts.minute) >= MORNING_END for ts in t]]
    if after.empty:
        return None

    in_pos = False
    entry = 0.0
    qty = 0.0                 # 남은 비중 (1.0 = 전량)
    peak = 0.0
    hit = set()               # 체결된 사다리 레벨
    exits = []                # (frac, price, reason)

    for ts, r in after.iterrows():
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])

        if not in_pos:
            # 눌림 진입: 저가가 트리거까지 닿으면 체결
            if l <= entry_trigger:
                # 갭하락으로 시가가 트리거보다 낮으면 시가 체결
                fill = min(o, entry_trigger) if o < entry_trigger else entry_trigger
                # 단, 갭으로 시가가 이미 손절선 이하면 진입 직후 손절될 자리 → 시가 진입 후 같은봉 손절 처리
                in_pos = True
                entry = fill
                stop_price = entry * (1 - STOP_PCT / 100)   # 진입가 -3% 고정 손절
                qty = 1.0
                peak = fill
            continue

        # 보유 중
        peak = max(peak, h)
        peak_pct = (peak / entry - 1) * 100

        # 1) -3% 고정 손절 (최우선)
        if l <= stop_price:
            px = min(o, stop_price) if o < stop_price else stop_price
            exits.append((qty, px, f"손절(-{STOP_PCT:g}%)"))
            qty = 0.0
            break

        # 2) 트레일링 (고점 -2%, peak가 ARM 이상일 때)
        if peak_pct >= TRAIL_ARM_PCT:
            trail_px = peak * (1 - TRAIL_GIVEBACK)
            if l <= trail_px:
                px = min(o, trail_px) if o < trail_px else trail_px
                exits.append((qty, px, f"트레일링(고점-{TRAIL_GIVEBACK*100:.0f}%)"))
                qty = 0.0
                break

        # 3) 익절 사다리
        for lvl, frac in LADDER:
            if lvl in hit:
                continue
            target = entry * (1 + lvl / 100)
            if h >= target:
                exits.append((min(frac, qty), target, f"+{lvl:g}%"))
                qty -= frac
                hit.add(lvl)
        # +5% 전량
        if qty > 0 and h >= entry * (1 + FULL_OUT_PCT / 100):
            exits.append((qty, entry * (1 + FULL_OUT_PCT / 100), f"+{FULL_OUT_PCT:g}% 전량"))
            qty = 0.0
            break

    if not in_pos:
        return None

    # 장 마감 청산
    if qty > 1e-9:
        last_c = float(after["close"].iloc[-1])
        exits.append((qty, last_c, "마감청산"))
        qty = 0.0

    # 손익 계산 (수수료 반영)
    proceeds = sum(f * px * (1 - SELL_COMM) for f, px, _ in exits)
    cost = entry * (1 + BUY_COMM)
    pnl_pct = (proceeds / cost - 1) * 100
    return {
        "date": str(after.index[0].date()),
        "entry": entry, "swing": swing, "stop": stop_price,
        "pnl_pct": pnl_pct, "exits": exits,
        "reasons": [r for _, _, r in exits],
    }


def _backtest(df: pd.DataFrame) -> list[dict]:
    trades = []
    for d in sorted(set(df.index.date)):
        day = df[df.index.date == d]
        tr = _simulate_day(day)
        if tr:
            trades.append(tr)
    return trades


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    # 누적(매일 동일 베팅 가정, 복리)
    eq = 1.0
    curve = []
    for p in pnls:
        eq *= (1 + p / 100)
        curve.append(eq)
    peak = -1e9
    mdd = 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, (e / peak - 1) * 100)
    return {
        "n": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "avg": sum(pnls) / len(pnls),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "total": (eq - 1) * 100,
        "mdd": mdd,
        "best": max(pnls), "worst": min(pnls),
    }


def main():
    sym_arg = sys.argv[1] if len(sys.argv) > 1 else "005930,000660,035420,000270,005380"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in sym_arg.split(",")]

    print(f"대장주 5분봉 스윙 전략 백테스트 | 종목 {symbols} | 기간 {period}")
    print(f"진입 전저점+{ENTRY_ABOVE*100:g}% / 손절 진입가-{STOP_PCT:g}% / 사다리 +1·2·3·4%×20% +5%전량 "
          f"/ 트레일 고점-{TRAIL_GIVEBACK*100:g}%(arm +{TRAIL_ARM_PCT:g}%)")
    W = 90
    all_trades = []
    print("=" * W)
    print(f"{'종목':<10} {'거래':>4} {'진입일수':>7} {'승률':>6} {'평균':>7} "
          f"{'평균익':>7} {'평균손':>7} {'누적':>8} {'MDD':>7} {'최고':>7} {'최악':>7}")
    print("-" * W)
    for sym in symbols:
        try:
            df = _download(sym, period)
        except Exception as e:
            print(f"{sym:<10} 다운로드 실패 — {e}")
            continue
        days = len(set(df.index.date))
        trades = _backtest(df)
        all_trades += trades
        s = _stats(trades)
        if s["n"] == 0:
            print(f"{sym:<10} {0:>4} {days:>7} {'-':>6} (진입 없음)")
            continue
        print(f"{sym:<10} {s['n']:>4} {days:>7} {s['wr']:>5.0f}% {s['avg']:>+6.2f}% "
              f"{s['avg_win']:>+6.2f}% {s['avg_loss']:>+6.2f}% {s['total']:>+7.2f}% "
              f"{s['mdd']:>6.1f}% {s['best']:>+6.2f}% {s['worst']:>+6.2f}%")
    print("-" * W)
    g = _stats(all_trades)
    if g["n"]:
        print(f"{'합계':<10} {g['n']:>4} {'':>7} {g['wr']:>5.0f}% {g['avg']:>+6.2f}% "
              f"{g['avg_win']:>+6.2f}% {g['avg_loss']:>+6.2f}% {g['total']:>+7.2f}% "
              f"{g['mdd']:>6.1f}% {g['best']:>+6.2f}% {g['worst']:>+6.2f}%")
    # 청산 사유 분포
    from collections import Counter
    rc = Counter(r for t in all_trades for r in t["reasons"])
    print("\n청산 사유 분포:", dict(rc))
    print("=" * W)
    print("판정: 승률·평균손익·누적이 양(+)이고 최악거래/MDD가 감내 범위면 라이브 검토")


if __name__ == "__main__":
    main()
