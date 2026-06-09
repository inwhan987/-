"""대장주 콤보 전략 — 눌림목 OR 전고점 재돌파, 둘 중 먼저 뜨는 신호로 진입.

두 진입 신호를 같이 돌려 "한 개라도 맞으면" 진입한다(하루 1회).

  공통
    · 대상   : 9:30 선별된 그날의 대장주
    · 고점   : 9:00~9:30 최고가 (pre_high)
    · 익절   : 진입가 × (1 + TP/100)  →  +4%
    · 마감   : 14:55 종가 청산
    · 상한가 : 목표가가 상한가 위면 해당 신호 진입 스킵

  신호 A — 눌림목(pullback)
    · 9:30 이후 W=2 스윙저점 중 pre_high 대비 MAX_PULL(7%) 이내인 것
    · 진입 = 확정봉 종가,  손절 = 스윙저점 × (1 - STOP_BUF)  →  -1%

  신호 B — 전고점 재돌파(breakout)
    · 9:30 이후 5분봉 *종가*가 pre_high 를 초과(돌파)하는 첫 봉
    · 진입 = 돌파봉 종가,  손절 = 진입가 × (1 - STOP_BUF)  →  -1%

  → 두 신호 중 먼저 충족된 봉에서 진입. 동일봉이면 눌림목 우선.

사용:
  python backtest_leader_combo.py 종목:날짜 [종목:날짜 ...]
  예) python backtest_leader_combo.py 089030.KQ:2026-06-09
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
STOP_BUF = 0.01   # 손절 버퍼 (-1%)
TP       = 4.0    # 익절 +4%
MAX_PULL = 0.07   # 눌림목: 9:00~9:30 고점 대비 최대 눌림 허용 비율 (7%)
TRADE_START = (9, 30)   # 대장주 선별 후 감시 시작
CLOSE_TIME  = (14, 55)  # 미청산 시 강제 마감
MAX_TRADES  = 2         # 하루 최대 진입 횟수 (손절 후 재신호 시 재진입 허용)

BUY_COMM  = 0.00015
SELL_COMM = 0.00195


def _download_day(ticker: str, date: str):
    """(당일 5분봉, 전일종가) 튜플 반환."""
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
    target = pd.to_datetime(date).date()
    day = df[df.index.date == target]
    if day.empty:
        raise ValueError(f"no bars on {date} for {ticker}")
    prev = df[df.index.date < target]
    prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None
    return day, prev_close


def simulate(day: pd.DataFrame, prev_close: float | None = None) -> dict:
    bars   = list(day.iterrows())
    lows   = [float(r["low"])   for _, r in bars]
    highs  = [float(r["high"])  for _, r in bars]
    opens  = [float(r["open"])  for _, r in bars]
    closes = [float(r["close"]) for _, r in bars]
    times  = [ts for ts, _ in bars]
    n = len(bars)

    # ── Phase 1: 9:00~9:30 고점 + 상한가 ─────────────────────────────
    pre_highs = [highs[j] for j in range(n)
                 if (times[j].hour, times[j].minute) < TRADE_START]
    if not pre_highs:
        return {"entered": False, "reason": "9:00~9:30 데이터 없음"}
    pre_high    = max(pre_highs)
    floor       = pre_high * (1 - MAX_PULL)
    upper_limit = prev_close * 1.30 if prev_close else None

    def _entry_ok(entry_px: float) -> bool:
        """목표가가 상한가 위면 진입 불가."""
        if not upper_limit:
            return True
        return entry_px * (1 + TP / 100) <= upper_limit

    def _find_trade(start: int):
        """start 봉부터 첫 진입 신호를 찾아 한 거래를 끝까지 시뮬. (trade, exit_idx)."""
        for j in range(start, n):
            ts = times[j]
            if (ts.hour, ts.minute) < TRADE_START:
                continue
            o, h, l, c = opens[j], highs[j], lows[j], closes[j]
            is_last = (j == n - 1) or (ts.hour, ts.minute) >= CLOSE_TIME

            # 신호 A: 눌림목 (W=2 스윙저점 확정)
            sig = None
            ref = stop = 0.0
            i = j - W
            if (i >= W and
                    all(lows[i] <= lows[i - k] for k in range(1, W + 1)) and
                    all(lows[i] <= lows[i + k] for k in range(1, W + 1)) and
                    lows[i] >= floor and _entry_ok(c)):
                sig, ref, stop = "눌림목", lows[i], lows[i] * (1 - STOP_BUF)

            # 신호 B: 전고점 재돌파 (종가가 pre_high 초과)
            if sig is None and c > pre_high and _entry_ok(c):
                sig, ref, stop = "재돌파", pre_high, c * (1 - STOP_BUF)

            if sig is None:
                continue

            entry, entry_ts = c, ts
            tp_px = entry * (1 + TP / 100)
            if l <= stop:
                return _result(sig, entry, ref, stop, entry_ts, stop, "손절(진입봉)", ts), j
            if h >= tp_px:
                return _result(sig, entry, ref, stop, entry_ts, tp_px,
                               f"+{TP:g}%익절(진입봉)", ts), j
            if is_last:
                return _result(sig, entry, ref, stop, entry_ts, c, "마감청산", ts), j

            for k in range(j + 1, n):
                tk = times[k]
                ok, hk, lk, ck = opens[k], highs[k], lows[k], closes[k]
                last_k = (k == n - 1) or (tk.hour, tk.minute) >= CLOSE_TIME
                if lk <= stop:
                    px = min(ok, stop) if ok < stop else stop
                    return _result(sig, entry, ref, stop, entry_ts, px, "손절", tk), k
                if hk >= tp_px:
                    return _result(sig, entry, ref, stop, entry_ts, tp_px,
                                   f"+{TP:g}%익절", tk), k
                if last_k:
                    return _result(sig, entry, ref, stop, entry_ts, ck, "마감청산", tk), k
            return _result(sig, entry, ref, stop, entry_ts, closes[-1],
                           "마감청산", times[-1]), n - 1
        return None, n

    # ── Phase 2: 손절 후 재신호 시 재진입(MAX_TRADES회) ────────────────
    trades = []
    cursor = 0
    while len(trades) < MAX_TRADES:
        tr, exit_idx = _find_trade(cursor)
        if tr is None:
            break
        trades.append(tr)
        if "익절" in tr["reason"] or "마감" in tr["reason"]:
            break          # 익절/마감으로 끝났으면 그날 종료
        cursor = exit_idx + 1   # 손절 → 다음 봉부터 재신호 탐색

    if not trades:
        return {"entered": False,
                "reason": f"눌림목/재돌파 신호 없음 (고점 {pre_high:,.0f})"}
    return {"entered": True, "trades": trades}


def _result(sig, entry, ref, stop, entry_ts, exit_px, reason, exit_ts) -> dict:
    net = (exit_px * (1 - SELL_COMM) / (entry * (1 + BUY_COMM)) - 1) * 100
    return {
        "entered": True, "signal": sig, "entry": entry, "ref": ref, "stop": stop,
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

    print(f"대장주 콤보(눌림목 OR 재돌파)  W={W}({W*5}분)  손절-{STOP_BUF*100:g}%  "
          f"익절+{TP:g}%  눌림한도{MAX_PULL*100:g}%  최대{MAX_TRADES}회")
    print("=" * 72)

    nets = []
    for pair in pairs:
        if ":" not in pair:
            print(f"  ⚠ 형식오류: {pair} (종목:날짜)")
            continue
        ticker, date = pair.split(":", 1)
        try:
            day, prev_close = _download_day(ticker, date)
            r = simulate(day, prev_close)
        except Exception as e:
            print(f"  ⚠ {pair}: {e}")
            continue

        if not r.get("entered"):
            print(f"  {ticker} {date}  미진입 — {r.get('reason','')}")
            continue

        day_net = 0.0
        print(f"  {ticker} {date}")
        for idx, t in enumerate(r["trades"], 1):
            et = t["entry_ts"].strftime("%H:%M")
            xt = t["exit_ts"].strftime("%H:%M")
            day_net += t["net"]
            print(f"   #{idx} [{t['signal']}] 진입 {et} @{t['entry']:,.0f} "
                  f"(기준 {t['ref']:,.0f}, 손절 {t['stop']:,.0f})")
            print(f"        청산 {xt} @{t['exit_px']:,.0f}  {t['reason']}  "
                  f"→ net {t['net']:+.2f}%")
        nets.append(day_net)
        print(f"     ▶ 하루 합계 net {day_net:+.2f}%")
        print("-" * 72)

    if nets:
        wins = sum(1 for x in nets if x > 0)
        print(f"\n총 {len(nets)}일  {wins}승 {len(nets)-wins}패  "
              f"합계 net {sum(nets):+.2f}%  평균 {sum(nets)/len(nets):+.2f}%")


if __name__ == "__main__":
    main()
