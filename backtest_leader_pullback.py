"""대장주 눌림목 전략 — 특정 (종목, 날짜) 하루 검증.

전략 (사용자 설계):
  · 대상   : 9:30 선별된 그날의 대장주
  · 고점   : 9:00~9:30 최고가 (pre_high)
  · 눌림목 : 9:30 이후 W=2 스윙저점 중 pre_high 대비 MAX_PULL(7%) 이내인 것만 유효
  · 진입   : 유효 스윙저점 확정봉 종가 (실전: 다음봉 시가 주문)
  · 손절   : 스윙저점 × (1 - STOP_BUF)  →  -1%
  · 익절   : 진입가 × (1 + TP/100)      →  +4%
  · 마감   : 14:55 종가 청산

진입봉 동일봉 손절/익절 체크 포함 (같은 봉에서 진입+손절 동시 발생 가능).

사용:
  python backtest_leader_pullback.py 종목:날짜 [종목:날짜 ...]
  예) python backtest_leader_pullback.py 009150.KS:2026-05-26 064400.KS:2026-06-01
  코드만 주면 .KS 가정.
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
W        = 2      # 스윙저점 좌우 비교 봉수 (W=2 → 10분 딜레이)
STOP_BUF = 0.01   # 손절 = 스윙저점 × 0.99  (-1%)
TP       = 4.0    # 익절 +4%
MAX_PULL = 0.07   # 9:00~9:30 고점 대비 최대 눌림 허용 비율 (7%)
                  # 그 이상 빠진 스윙저점은 과매도로 간주, 진입 제외
TRADE_START = (9, 30)   # 대장주 선별 후 감시 시작
CLOSE_TIME  = (14, 55)  # 미청산 시 강제 마감

BUY_COMM  = 0.00015
SELL_COMM = 0.00195


def _download_day(ticker: str, date: str) -> pd.DataFrame:
    import yfinance as yf
    if "." not in ticker:
        ticker = f"{ticker}.KS"
    df = yf.download(ticker, period="60d", interval="5m",
                     auto_adjust=True, progress=False, timeout=30)
    if df.empty:
        raise ValueError(f"no data {ticker}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Seoul")
    day = df[df.index.date == pd.to_datetime(date).date()]
    if day.empty:
        raise ValueError(f"no bars on {date} for {ticker}")
    return day


def simulate(day: pd.DataFrame) -> dict:
    bars   = list(day.iterrows())
    lows   = [float(r["low"])   for _, r in bars]
    highs  = [float(r["high"])  for _, r in bars]
    opens  = [float(r["open"])  for _, r in bars]
    closes = [float(r["close"]) for _, r in bars]
    times  = [ts for ts, _ in bars]
    n = len(bars)

    # ── Phase 1: 9:00~9:30 고점 파악 ───────────────────────────────────
    pre_highs = [highs[j] for j in range(n)
                 if (times[j].hour, times[j].minute) < TRADE_START]
    if not pre_highs:
        return {"entered": False, "reason": "9:00~9:30 데이터 없음"}
    pre_high = max(pre_highs)
    floor    = pre_high * (1 - MAX_PULL)   # 이 값 아래 스윙저점은 제외

    # ── Phase 2: 9:30 이후 W=2 스윙저점 탐색 → 유효 눌림목만 진입 ──────
    in_pos   = False
    entry = ref = stop = 0.0
    entry_ts = None

    for j in range(n):
        ts = times[j]
        if (ts.hour, ts.minute) < TRADE_START:
            continue
        o, h, l, c = opens[j], highs[j], lows[j], closes[j]
        is_last = (j == n - 1) or (ts.hour, ts.minute) >= CLOSE_TIME

        if not in_pos:
            i = j - W
            if i >= W and (all(lows[i] <= lows[i - k] for k in range(1, W + 1)) and
                           all(lows[i] <= lows[i + k] for k in range(1, W + 1))):
                if lows[i] >= floor:   # ★ 고점 대비 MAX_PULL 이내만 유효
                    ref      = lows[i]
                    entry    = c                      # 확정봉 종가 (실전: 다음봉 시가)
                    stop     = ref * (1 - STOP_BUF)
                    entry_ts = ts
                    in_pos   = True
                    # 진입봉에서 즉시 손절/익절 체크
                    if l <= stop:
                        return _result(entry, ref, stop, entry_ts, stop,
                                       "손절(진입봉)", ts)
                    if h >= entry * (1 + TP / 100):
                        return _result(entry, ref, stop, entry_ts,
                                       entry * (1 + TP / 100), f"+{TP:g}%익절(진입봉)", ts)
            continue

        # 보유 중
        if l <= stop:
            px = min(o, stop) if o < stop else stop
            return _result(entry, ref, stop, entry_ts, px, "손절", ts)
        if h >= entry * (1 + TP / 100):
            return _result(entry, ref, stop, entry_ts,
                           entry * (1 + TP / 100), f"+{TP:g}%익절", ts)
        if is_last:
            return _result(entry, ref, stop, entry_ts, c, "마감청산", ts)

    if not in_pos:
        return {"entered": False, "reason": f"유효 스윙저점 없음 (고점{pre_high:,.0f} 대비 {MAX_PULL*100:.0f}% 이내)"}
    return _result(entry, ref, stop, entry_ts,
                   float(day["close"].iloc[-1]), "마감청산", times[-1])


def _result(entry, ref, stop, entry_ts, exit_px, reason, exit_ts) -> dict:
    net = (exit_px * (1 - SELL_COMM) / (entry * (1 + BUY_COMM)) - 1) * 100
    return {
        "entered": True, "entry": entry, "ref": ref, "stop": stop,
        "entry_ts": entry_ts, "exit_px": exit_px, "exit_ts": exit_ts,
        "reason": reason,
        "gross": (exit_px / entry - 1) * 100,
        "net": net,
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pairs = sys.argv[1:]
    if not pairs:
        print(__doc__)
        return

    print(f"대장주 눌림목 전략  W={W}({W*5}분)  손절-{STOP_BUF*100:g}%  "
          f"익절+{TP:g}%  눌림{MAX_PULL*100:.0f}%이내  선별{TRADE_START[0]:02d}:{TRADE_START[1]:02d}~")
    SEP = "=" * 90
    print(SEP)

    rows = []
    for pair in pairs:
        if ":" not in pair:
            print(f"형식오류(종목:날짜): {pair}"); continue
        tk, date = pair.split(":", 1)
        try:
            day = _download_day(tk, date)
        except Exception as e:
            print(f"{tk} {date}  다운로드 실패 — {e}"); continue

        op = float(day["open"].iloc[0]); hi = float(day["high"].max())
        lo = float(day["low"].min());    cl = float(day["close"].iloc[-1])
        pre_high = max(float(r["high"]) for ts, r in day.iterrows()
                       if (ts.hour, ts.minute) < TRADE_START)
        print(f"■ {tk}  {date}  "
              f"시{op:,.0f} 고{hi:,.0f} 저{lo:,.0f} 종{cl:,.0f}  "
              f"(일중 {(cl/op-1)*100:+.1f}%)  9:30전고점 {pre_high:,.0f}")

        res = simulate(day)
        if not res["entered"]:
            print(f"   → 미진입: {res['reason']}\n")
            rows.append(None); continue

        print(f"   스윙저점 {res['ref']:,.0f}  손절 {res['stop']:,.0f}  "
              f"진입 {res['entry_ts'].strftime('%H:%M')} @ {res['entry']:,.0f}  "
              f"목표 {res['entry']*(1+TP/100):,.0f}")
        print(f"   청산 {res['exit_ts'].strftime('%H:%M')} @ {res['exit_px']:,.0f}  "
              f"[{res['reason']}]  gross {res['gross']:+.2f}%  net {res['net']:+.2f}%\n")
        rows.append(res)

    done = [r for r in rows if r]
    print("-" * 90)
    if done:
        nets = [r["net"] for r in done]
        wins = sum(1 for x in nets if x > 0)
        print(f"진입 {len(done)}건 / 미진입 {len(rows)-len(done)}건 | "
              f"승 {wins}/{len(done)} | 평균 {sum(nets)/len(nets):+.2f}% | 합계 {sum(nets):+.2f}%")
    else:
        print("진입 없음")
    print(SEP)


if __name__ == "__main__":
    main()
