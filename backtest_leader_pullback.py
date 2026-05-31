"""대장주 눌림목 전략 — 특정 (종목, 날짜) 하루 검증.

전략 (사용자 설계):
  · 대상   : 장 시작 후 선별된 그날의 대장주
  · 진입모드(ENTRY_MODE):
      'pullback' — 전저가 × (1+ENTRY_ABOVE) 까지 다시 눌릴 때 매수 (기존)
      'confirm'  — 스윙저점 확정봉 종가에 즉시 매수 (빠른 진입, 기본값)
  · 스윙저점: 좌우 W봉보다 낮은 골, i+W봉에서 확정 → 룩어헤드 없음
  · 손절   : 전저가 × (1-STOP_BUF)
  · 익절   : 진입가 +TP% 단일 목표
  · 마감   : 미도달 시 종가 청산

사용:
  python backtest_leader_pullback.py 종목:날짜 [종목:날짜 ...]
  예) python backtest_leader_pullback.py 009150.KS:2026-05-26 066970.KS:2026-05-28
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
W           = 2           # 스윙 저점 좌우 비교 봉수
ENTRY_MODE  = "confirm"   # "confirm"(확정봉 종가 즉시) | "pullback"(전저가+1% 대기)
ENTRY_ABOVE = 0.01        # pullback 모드: 전저가 +N% 진입
STOP_BUF    = 0.01        # 손절 = 전저가 -1% (버퍼)
TP          = 5.0         # 익절 +5%
ENTRY_START = (9, 10)     # 선별 직후(9:10~) 즉시 눌림목 감시 시작

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
    bars = list(day.iterrows())
    lows   = [float(r["low"])   for _, r in bars]
    highs  = [float(r["high"])  for _, r in bars]
    opens  = [float(r["open"])  for _, r in bars]
    closes = [float(r["close"]) for _, r in bars]
    times  = [ts for ts, _ in bars]
    n = len(bars)

    def find_new_swing(j: int):
        """j봉에서 새로 확정된 스윙저점 반환 (j == ref_i + W 인 봉만). 없으면 None."""
        i = j - W
        if i < W:
            return None
        if (all(lows[i] <= lows[i - k] for k in range(1, W + 1)) and
                all(lows[i] <= lows[i + k] for k in range(1, W + 1))):
            return (i, lows[i])
        return None

    in_pos = False
    entry = ref = stop = 0.0
    entry_ts = None
    last_ref = None          # 가장 최근 확정 스윙저점 (pullback 모드에서 갱신)

    for j in range(n):
        ts = times[j]
        if (ts.hour, ts.minute) < ENTRY_START:
            continue
        o, h, l, c = opens[j], highs[j], lows[j], closes[j]

        if not in_pos:
            new_sw = find_new_swing(j)

            if ENTRY_MODE == "confirm":
                # ── 확정봉 종가 즉시 진입 ──────────────────────────────────
                # 이 봉에서 새 스윙저점이 확정되면 → 이 봉 종가에 바로 체결
                if new_sw is not None:
                    ref_i, ref_v = new_sw
                    entry  = c                        # 확정봉 종가
                    ref    = ref_v
                    stop   = ref_v * (1 - STOP_BUF)
                    entry_ts = ts
                    in_pos = True
            else:
                # ── pullback: 전저가+1% 대기 ──────────────────────────────
                if new_sw is not None:
                    last_ref = new_sw[1]              # 새 스윙저점 갱신
                if last_ref is None:
                    continue
                trig = last_ref * (1 + ENTRY_ABOVE)
                if l <= trig:
                    entry    = min(o, trig) if o < trig else trig
                    ref      = last_ref
                    stop     = last_ref * (1 - STOP_BUF)
                    entry_ts = ts
                    in_pos   = True
            continue

        # ── 보유 중: 손절 / 익절 ────────────────────────────────────────
        if l <= stop:
            px = min(o, stop) if o < stop else stop
            return _result(day, entry, ref, stop, entry_ts, px,
                           f"손절(전저가-{STOP_BUF*100:g}%)", ts)
        if h >= entry * (1 + TP / 100):
            return _result(day, entry, ref, stop, entry_ts,
                           entry * (1 + TP / 100), f"+{TP:g}%익절", ts)

    if not in_pos:
        return {"entered": False}
    last_c = float(day["close"].iloc[-1])
    return _result(day, entry, ref, stop, entry_ts, last_c, "마감청산", times[-1])


def _result(day, entry, ref, stop, entry_ts, exit_px, reason, exit_ts) -> dict:
    gross = (exit_px / entry - 1) * 100
    net = (exit_px * (1 - SELL_COMM) / (entry * (1 + BUY_COMM)) - 1) * 100
    return {
        "entered": True, "entry": entry, "ref": ref, "stop": stop,
        "entry_ts": entry_ts, "exit_px": exit_px, "exit_ts": exit_ts,
        "reason": reason, "gross": gross, "net": net,
    }


def main():
    pairs = sys.argv[1:]
    if not pairs:
        print(__doc__)
        return
    mode_str = (f"확정봉종가즉시" if ENTRY_MODE == "confirm"
                else f"전저가+{ENTRY_ABOVE*100:g}%대기")
    print(f"대장주 눌림목 전략 | 진입 [{mode_str}] / 손절 전저가-{STOP_BUF*100:g}% "
          f"/ 익절 +{TP:g}% / 진입검토 {ENTRY_START[0]:02d}:{ENTRY_START[1]:02d}~")
    W2 = 96
    print("=" * W2)
    rows = []
    for pair in pairs:
        if ":" not in pair:
            print(f"형식오류(종목:날짜): {pair}")
            continue
        tk, date = pair.split(":", 1)
        try:
            day = _download_day(tk, date)
        except Exception as e:
            print(f"{tk} {date}  다운로드 실패 — {e}")
            continue
        op = float(day["open"].iloc[0]); hi = float(day["high"].max())
        lo = float(day["low"].min()); cl = float(day["close"].iloc[-1])
        day_chg = (cl / op - 1) * 100
        print(f"■ {tk}  {date}  시{op:.0f} 고{hi:.0f} 저{lo:.0f} 종{cl:.0f}  (일중 {day_chg:+.1f}%)")
        res = simulate(day)
        if not res["entered"]:
            print(f"   → 진입 없음 (전저가+{ENTRY_ABOVE*100:g}%까지 눌림 미발생)\n")
            rows.append((tk, date, None))
            continue
        print(f"   진입 {res['entry_ts'].strftime('%H:%M')} @ {res['entry']:.0f}  "
              f"(전저가 {res['ref']:.0f}, 손절 {res['stop']:.0f}, 목표 {res['entry']*1.05:.0f})")
        print(f"   청산 {res['exit_ts'].strftime('%H:%M')} @ {res['exit_px']:.0f}  [{res['reason']}]  "
              f"손익 gross {res['gross']:+.2f}% / net {res['net']:+.2f}%\n")
        rows.append((tk, date, res))
    # 요약
    done = [r for _, _, r in rows if r and r["entered"]]
    print("-" * W2)
    if done:
        nets = [r["net"] for r in done]
        wins = sum(1 for x in nets if x > 0)
        print(f"진입 {len(done)}건 / 미진입 {len(rows)-len(done)}건 | "
              f"승 {wins}/{len(done)} | 평균 net {sum(nets)/len(nets):+.2f}% | "
              f"합 net {sum(nets):+.2f}%")
    else:
        print("진입 사례 없음")
    print("=" * W2)


if __name__ == "__main__":
    main()
