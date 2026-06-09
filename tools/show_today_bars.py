"""오늘 KIS 실봉을 N분봉으로 합성해 OHLCV 표로 출력 (Pi 점검용).

사용법 (Pi 에서):
    cd ~/stock-bot
    python tools/show_today_bars.py                 # 첫 운용종목, 5분봉
    python tools/show_today_bars.py 005930.KS        # 종목 지정
    python tools/show_today_bars.py 005930.KS 3      # 3분봉
    IV=10 python tools/show_today_bars.py            # 환경변수로 간격 지정

컬럼
----
open/high/low/close/volume : KIS 1분봉을 origin=start_day(09:00) 정렬로 N분 합성한 실 OHLC
value  : 누적 거래대금 = Σ(close × volume)
change : 직전 봉 대비 종가 등락률
"""
from __future__ import annotations

import os
import sys

import pandas as pd

from stock_bot.broker.kis import KISBroker
from stock_bot.config.settings import settings


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    symbol = args[0] if args else (settings.symbols[0] if settings.symbols else "005930.KS")
    if len(args) >= 2:
        interval = int(args[1])
    else:
        interval = int(os.environ.get("IV", str(settings.live_candle_minutes)))

    # 마감 후 실행 시 KIS 가 현재시각까지 종가로 패딩한 가짜 봉을 잘라낸다.
    # (정규장 09:00~15:30 + 종가단일가 15:30 까지만 진짜 데이터)
    keep_session_only = os.environ.get("ALL", "") != "1"

    broker = KISBroker()
    bars = broker.get_minute_ohlcv_today(symbol, interval_min=interval)  # newest-first
    if not bars:
        print(f"[!] {symbol} {interval}분봉: 데이터 없음 (장중 아님 / 인증 / 네트워크 확인)")
        return 1

    rows = list(reversed(bars))  # 오름차순(과거→현재)
    if keep_session_only:
        rows = [r for r in rows if str(r["time"]) <= "153000"]
        if not rows:
            print(f"[!] {symbol}: 정규장(≤15:30) 봉 없음. 전체 보려면 ALL=1")
            return 1
    idx = pd.to_datetime(
        [f"{r['date']} {r['time']}" for r in rows], format="%Y%m%d %H%M%S"
    )
    df = pd.DataFrame(
        {
            "open": [float(r["open"]) for r in rows],
            "high": [float(r["high"]) for r in rows],
            "low": [float(r["low"]) for r in rows],
            "close": [float(r["close"]) for r in rows],
            "volume": [int(r["volume"]) for r in rows],
        },
        index=idx,
    )
    df.index.name = "Date"
    df["value"] = (df["close"] * df["volume"]).cumsum()
    df["change"] = df["close"].pct_change()

    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print(f"\n[{symbol}] {interval}분봉 · 오늘 {len(df)}개 봉 (KIS 실봉 합성)\n")
    print(
        df.to_string(
            float_format=lambda x: f"{x:.6f}" if abs(x) < 1 else f"{x:.1f}",
            formatters={"value": lambda x: f"{x:.6e}"},
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
