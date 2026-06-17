"""대장주 눌림목 전략 — 특정 (종목, 날짜) 하루 검증.

전략 (사용자 설계):
  · 대상   : 9:30 선별된 그날의 대장주
  · 고점   : 9:00~9:30 최고가 (pre_high)
  · 눌림목 : 9:30 이후 W=2 스윙저점 중 pre_high 대비 MAX_PULL(7%) 이내인 것만 유효
  · 진입   : 유효 스윙저점 확정봉 종가 (실전: 다음봉 시가 주문)
  · 손절   : 스윙저점 × (1 - STOP_BUF)  →  -1.5% (3분봉, 06-09 스윕 확정)
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
# env 로 오버라이드 가능(로컬 스윕용):  BT_W / BT_STOP / BT_TP / BT_PULL / BT_MAXTRADES
W          = int(os.environ.get("BT_W", 2))         # 스윙저점 좌우 비교 봉수 (W=2, 3분봉이면 6분)
STOP_BUF   = float(os.environ.get("BT_STOP", 0.015)) # 손절 = 스윙저점 × (1-STOP_BUF) — 06-09 스윕 확정 -1.5%
TP         = float(os.environ.get("BT_TP", 4.0))    # 익절 +TP%
MAX_PULL   = float(os.environ.get("BT_PULL", 0.07)) # 9:00~9:30 고점 대비 최대 눌림 허용
MAX_TRADES = int(os.environ.get("BT_MAXTRADES", 1)) # 하루 최대 진입 횟수(손절 후 재진입)
LAG        = int(os.environ.get("BT_LAG", 0))       # 진입 지연 봉수(0=확정봉 종가, ≥1=N봉 뒤 시가)
FRESH      = int(os.environ.get("BT_FRESH", 0))     # 신선도 컷(분): 전고점 형성 후 N분 초과 눌림목 보류. 0=무제한
NODMG      = os.environ.get("BT_NODMG", "1") == "1"  # 붕괴종목 컷(기본 켬, 06-11): 전고점 후 진입 전 floor 깨면 보류. BT_NODMG=0 으로 끔
RECLAIM    = os.environ.get("BT_RECLAIM", "1") == "1" # 회복확인(기본 켬): 확정봉 종가 > 직전봉 고가일 때만 진입. BT_RECLAIM=0 으로 끔
STOP_BASE  = os.environ.get("BT_STOPBASE", "ref")    # 손절 기준: ref=스윙저점(기본) / entry=진입가
# 선별 시각: BT_START="10:00" → 9:00~10:00 전고점, 10:00 이후 진입 감시.
# BT_START 미지정 시 날짜별 data/leader_picks/날짜.json 의 selected_at 을 따름
# (재시도로 10시·11시에 선별된 날은 전고점 윈도우도 9:00~그 시각으로 자동 확장).
_bt_start_env = os.environ.get("BT_START", "")
if _bt_start_env:
    _ts = _bt_start_env.split(":")
    TRADE_START = (int(_ts[0]), int(_ts[1]))   # 명시 지정 → 전 날짜 공통
else:
    TRADE_START = None                          # 날짜별 picks 선별시각 (없으면 9:30)
# 전고점 윈도우 시작(기본 9:00). BT_PHSTART="9:30" → 9:30~TRADE_START 고점만 눌림 기준
_phs = os.environ.get("BT_PHSTART", "9:00").split(":")
PREHIGH_START = (int(_phs[0]), int(_phs[1]))
CLOSE_TIME  = (14, 55)  # 미청산 시 강제 마감

BUY_COMM  = 0.00015
SELL_COMM = 0.00195


# 봉 간격: BT_INTERVAL = 3m(기본, 06-09 스윕 확정)/5m/2m/1m.  3m 은 yfinance 미제공 → 1m 받아 합성(최근 ~8일만).
BAR_INTERVAL = os.environ.get("BT_INTERVAL", "3m")


def _trade_start_for(date: str) -> tuple[int, int]:
    """해당 날짜의 감시 시작 시각. BT_START 명시 > picks 선별시각 > 9:30 순."""
    if TRADE_START is not None:
        return TRADE_START
    import json
    from pathlib import Path
    path = Path(__file__).parent / "data" / "leader_picks" / f"{date}.json"
    try:
        sel = json.loads(path.read_text(encoding="utf-8"))["selected_at"]
        hh, mm = sel.split(":")[:2]
        return (int(hh), int(mm))
    except Exception:
        return (9, 30)


def _download_day(ticker: str, date: str):
    """(당일 봉, 전일종가) 튜플 반환. BT_INTERVAL 에 따라 간격 결정."""
    import yfinance as yf
    if "." not in ticker:
        ticker = f"{ticker}.KS"

    iv = BAR_INTERVAL
    if iv == "3m":
        dl_iv, period, resample = "1m", "8d", "3min"   # 1분봉 합성(최근 ~8일만)
    elif iv == "2m":
        dl_iv, period, resample = "2m", "60d", None
    elif iv == "1m":
        dl_iv, period, resample = "1m", "8d", None
    else:
        dl_iv, period, resample = "5m", "60d", None

    df = yf.download(ticker, period=period, interval=dl_iv,
                     auto_adjust=True, progress=False, timeout=30)
    if df.empty:
        raise ValueError(f"no data {ticker}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Seoul")

    if resample:   # 1분봉 → N분봉 OHLC 합성 (장 시작 9:00 기준 정렬)
        df = (df.resample(resample, label="left", closed="left", origin="start_day")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
                .dropna(subset=["close"]))

    target = pd.to_datetime(date).date()
    day = df[df.index.date == target]
    if day.empty:
        raise ValueError(f"no bars on {date} for {ticker}")
    # 전일 마지막 종가 → 상한가 계산용
    prev = df[df.index.date < target]
    prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None
    return day, prev_close


def simulate(day: pd.DataFrame, prev_close: float | None = None,
             trade_start: tuple[int, int] = (9, 30)) -> dict:
    bars   = list(day.iterrows())
    lows   = [float(r["low"])   for _, r in bars]
    highs  = [float(r["high"])  for _, r in bars]
    opens  = [float(r["open"])  for _, r in bars]
    closes = [float(r["close"]) for _, r in bars]
    times  = [ts for ts, _ in bars]
    n = len(bars)

    # ── Phase 1: 9:00~선별시각 고점 파악 + 상한가 계산 ────────────────
    _in_phwin = lambda j: PREHIGH_START <= (times[j].hour, times[j].minute) < trade_start
    pre_highs = [highs[j] for j in range(n) if _in_phwin(j)]
    if not pre_highs:
        return {"entered": False, "reason": "전고점 윈도우 데이터 없음"}
    pre_high    = max(pre_highs)
    _ph_j       = max((j for j in range(n) if _in_phwin(j)), key=lambda j: highs[j])
    pre_high_ts = times[_ph_j]                                # 전고점이 찍힌 시각(신선도 기준)
    floor       = pre_high * (1 - MAX_PULL)
    upper_limit = prev_close * 1.30 if prev_close else None  # 상한가 가격

    # ── Phase 2: 9:30 이후 W 스윙저점 탐색 → 유효 눌림목 진입 ──────────
    def _find_trade(start: int):
        """start 봉부터 첫 유효 눌림목을 찾아 한 거래를 끝까지 시뮬. (trade, exit_idx)."""
        for j in range(start, n):
            ts = times[j]
            if (ts.hour, ts.minute) < trade_start:
                continue
            o, h, l, c = opens[j], highs[j], lows[j], closes[j]
            is_last = (j == n - 1) or (ts.hour, ts.minute) >= CLOSE_TIME

            i = j - W
            if not (i >= W and
                    all(lows[i] <= lows[i - k] for k in range(1, W + 1)) and
                    all(lows[i] <= lows[i + k] for k in range(1, W + 1)) and
                    lows[i] >= floor):
                continue
            # 회복확인: 확정봉 종가가 직전봉 고가를 넘어야 진입(터치 아닌 반등 확인).
            if RECLAIM and not (closes[j] > highs[j - 1]):
                continue
            ref = lows[i]
            # 진입봉 결정: LAG=0 → 확정봉 종가 / LAG≥1 → N봉 뒤 시가(선별·체결 지연 반영)
            if LAG == 0:
                e, entry = j, closes[j]
            else:
                e = j + LAG
                if e >= n:
                    continue                       # 진입할 봉이 없음
                et = times[e]
                if (et.hour, et.minute) >= CLOSE_TIME:
                    continue                       # 마감 이후엔 진입 안 함
                entry = opens[e]                   # 다음봉 시가 체결
            entry_ts = times[e]
            # 신선도 컷: 전고점 형성 후 FRESH분 초과면 추세이탈로 보고 보류
            if FRESH > 0 and (entry_ts - pre_high_ts).total_seconds() / 60 > FRESH:
                continue
            # 붕괴종목 컷: 전고점 이후 진입봉 전까지 한번이라도 floor를 깼으면(이미 붕괴) 보류
            if NODMG and any(lows[k] < floor for k in range(_ph_j + 1, e)):
                continue
            stop  = (entry if STOP_BASE == "entry" else ref) * (1 - STOP_BUF)
            tp_px = entry * (1 + TP / 100)
            if upper_limit and tp_px > upper_limit:
                continue  # 목표가 상한가 위 → 다음 스윙저점 탐색
            e_last = (e == n - 1) or (times[e].hour, times[e].minute) >= CLOSE_TIME
            if lows[e] <= stop:
                return _result(entry, ref, stop, entry_ts, stop, "손절(진입봉)", times[e]), e
            if highs[e] >= tp_px:
                return _result(entry, ref, stop, entry_ts, tp_px,
                               f"+{TP:g}%익절(진입봉)", times[e]), e
            if e_last:
                return _result(entry, ref, stop, entry_ts, closes[e], "마감청산", times[e]), e
            for k in range(e + 1, n):
                tk = times[k]
                ok, hk, lk, ck = opens[k], highs[k], lows[k], closes[k]
                last_k = (k == n - 1) or (tk.hour, tk.minute) >= CLOSE_TIME
                if lk <= stop:
                    px = min(ok, stop) if ok < stop else stop
                    return _result(entry, ref, stop, entry_ts, px, "손절", tk), k
                if hk >= tp_px:
                    return _result(entry, ref, stop, entry_ts, tp_px,
                                   f"+{TP:g}%익절", tk), k
                if last_k:
                    return _result(entry, ref, stop, entry_ts, ck, "마감청산", tk), k
            return _result(entry, ref, stop, entry_ts, closes[-1],
                           "마감청산", times[-1]), n - 1
        return None, n

    trades = []
    cursor = 0
    while len(trades) < MAX_TRADES:
        tr, exit_idx = _find_trade(cursor)
        if tr is None:
            break
        trades.append(tr)
        if "익절" in tr["reason"] or "마감" in tr["reason"]:
            break               # 익절/마감으로 끝났으면 그날 종료
        cursor = exit_idx + 1   # 손절 → 다음 봉부터 재신호 탐색

    if not trades:
        return {"entered": False,
                "reason": f"유효 스윙저점 없음 (고점{pre_high:,.0f} 대비 {MAX_PULL*100:.0f}% 이내)"}
    return {"entered": True, "trades": trades}


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

    _start_lbl = (f"{TRADE_START[0]:02d}:{TRADE_START[1]:02d}~" if TRADE_START
                  else "날짜별 picks 선별시각~")
    _bar_min = int(BAR_INTERVAL.rstrip("m"))
    print(f"대장주 눌림목 전략  {BAR_INTERVAL}봉  W={W}({W*_bar_min}분)  손절-{STOP_BUF*100:g}%  "
          f"익절+{TP:g}%  눌림{MAX_PULL*100:.0f}%이내  최대{MAX_TRADES}회  "
          f"선별{_start_lbl}")
    SEP = "=" * 90
    print(SEP)

    rows = []
    for pair in pairs:
        if ":" not in pair:
            print(f"형식오류(종목:날짜): {pair}"); continue
        tk, date = pair.split(":", 1)
        try:
            day, prev_close = _download_day(tk, date)
        except Exception as e:
            print(f"{tk} {date}  다운로드 실패 — {e}"); continue

        trade_start = _trade_start_for(date)
        op = float(day["open"].iloc[0]); hi = float(day["high"].max())
        lo = float(day["low"].min());    cl = float(day["close"].iloc[-1])
        pre_highs = [float(r["high"]) for ts, r in day.iterrows()
                     if PREHIGH_START <= (ts.hour, ts.minute) < trade_start]
        pre_high = max(pre_highs) if pre_highs else 0
        upper_limit = prev_close * 1.30 if prev_close else 0
        print(f"■ {tk}  {date}  "
              f"시{op:,.0f} 고{hi:,.0f} 저{lo:,.0f} 종{cl:,.0f}  "
              f"(일중 {(cl/op-1)*100:+.1f}%)  "
              f"{trade_start[0]:02d}:{trade_start[1]:02d}전고점 {pre_high:,.0f}  "
              f"상한가 {upper_limit:,.0f}")

        res = simulate(day, prev_close, trade_start)
        if not res["entered"]:
            print(f"   → 미진입: {res['reason']}\n")
            rows.append(None); continue

        day_net = 0.0
        for idx, t in enumerate(res["trades"], 1):
            tag = f"#{idx} " if len(res["trades"]) > 1 else ""
            day_net += t["net"]
            print(f"   {tag}스윙저점 {t['ref']:,.0f}  손절 {t['stop']:,.0f}  "
                  f"진입 {t['entry_ts'].strftime('%H:%M')} @ {t['entry']:,.0f}  "
                  f"목표 {t['entry']*(1+TP/100):,.0f}")
            print(f"   {tag and '   '}청산 {t['exit_ts'].strftime('%H:%M')} @ {t['exit_px']:,.0f}  "
                  f"[{t['reason']}]  gross {t['gross']:+.2f}%  net {t['net']:+.2f}%")
        if len(res["trades"]) > 1:
            print(f"   ▶ 하루 합계 net {day_net:+.2f}%")
        print()
        rows.append(day_net)

    done = [r for r in rows if r is not None]
    print("-" * 90)
    if done:
        wins = sum(1 for x in done if x > 0)
        print(f"진입 {len(done)}일 / 미진입 {len(rows)-len(done)}일 | "
              f"승 {wins}/{len(done)} | 평균 {sum(done)/len(done):+.2f}% | 합계 {sum(done):+.2f}%")
    else:
        print("진입 없음")
    print(SEP)


if __name__ == "__main__":
    main()
