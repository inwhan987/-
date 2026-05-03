"""Supertrend m=2.5 vs m=3.0 신호 비교 시각화.

특정 날짜의 5분봉에서 두 설정이 언제 상승/하락 전환을 인식하는지 비교합니다.

사용:
  python tests/supertrend_compare.py              # 2026-04-23 (기본)
  python tests/supertrend_compare.py 2026-04-30   # 특정 날짜
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

# ── Supertrend 계산 ────────────────────────────────────────────────────────────
def calc_supertrend(df: pd.DataFrame, period: int, multiplier: float):
    """ATR 기반 Supertrend 계산. 반환: (direction, supertrend_line)"""
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    # ATR
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    # 기본 밴드
    hl2 = (high + low) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    # Supertrend 라인 & 방향 (1=상승, -1=하락)
    st   = pd.Series(index=df.index, dtype=float)
    prev_upper = upper.iloc[0]
    prev_lower = lower.iloc[0]
    direction  = pd.Series(index=df.index, dtype=int)

    # 초기값
    st.iloc[0]        = upper.iloc[0]
    direction.iloc[0] = -1

    for i in range(1, len(df)):
        cu = upper.iloc[i]
        cl = lower.iloc[i]
        pc = close.iloc[i - 1]
        cc = close.iloc[i]

        # 밴드 조정
        final_upper = cu if cu < prev_upper or pc > prev_upper else prev_upper
        final_lower = cl if cl > prev_lower or pc < prev_lower else prev_lower

        prev_dir = direction.iloc[i - 1]
        if prev_dir == -1:
            d =  1 if cc > final_upper else -1
        else:
            d = -1 if cc < final_lower else  1

        direction.iloc[i] = d
        st.iloc[i]        = final_lower if d == 1 else final_upper

        prev_upper = final_upper
        prev_lower = final_lower

    return direction, st


def _signal_label(d_cur, d_prev, has_pos):
    """방향 전환 → 신호 문자열."""
    if d_prev == -1 and d_cur == 1:
        return "BUY"
    if d_prev == 1 and d_cur == -1 and has_pos:
        return "SELL"
    return "↑" if d_cur == 1 else "↓"


def main() -> None:
    target_date = sys.argv[1] if len(sys.argv) > 1 else "2026-04-23"

    try:
        import yfinance as yf
    except ImportError:
        print("yfinance 미설치. pip install yfinance")
        sys.exit(1)

    print(f"\n데이터 다운로드: 005930.KS (5m, 60d)...", flush=True)
    df = yf.download("005930.KS", period="60d", interval="5m",
                     auto_adjust=True, progress=False)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)

    # ── 전체 데이터로 Supertrend 계산 (충분한 워밍업) ──────────────────────────
    dir25, st25 = calc_supertrend(df, period=7, multiplier=2.5)
    dir30, st30 = calc_supertrend(df, period=7, multiplier=3.0)

    # ── 해당 날짜 KST 필터링 ────────────────────────────────────────────────────
    df_kst = df.copy()
    df_kst.index = df_kst.index.tz_convert("Asia/Seoul")
    mask = df_kst.index.date == pd.Timestamp(target_date).date()
    if not mask.any():
        print(f"❌ {target_date} 데이터 없음 (휴장일이거나 날짜 형식 오류)")
        sys.exit(1)

    idx_day = df_kst.index[mask]
    day_df  = df_kst.loc[idx_day]
    # 원본 인덱스(UTC)에서 같은 위치 찾기
    utc_mask = df.index.isin(df.index[df_kst.index.isin(idx_day)])

    d25_day = dir25[utc_mask]
    d30_day = dir30[utc_mask]
    st25_day = st25[utc_mask]
    st30_day = st30[utc_mask]

    print(f"\n{'='*80}")
    print(f"  {target_date} (KST)  |  p=7  |  m=2.5 vs m=3.0 Supertrend 신호 비교")
    print(f"{'='*80}")
    hdr = f"{'시간':>6}  {'종가':>8}  {'m=2.5 ST':>10}  {'신호':>5}  |  {'m=3.0 ST':>10}  {'신호':>5}  | {'차이'}"
    print(hdr)
    print("-" * 80)

    pos25 = 0
    pos30 = 0
    buy_price25 = buy_price30 = 0.0

    for i, (ts, row) in enumerate(day_df.iterrows()):
        close = row["close"]
        t_str = ts.strftime("%H:%M")

        d25c = d25_day.iloc[i]
        d30c = d30_day.iloc[i]
        s25  = st25_day.iloc[i]
        s30  = st30_day.iloc[i]

        d25p = d25_day.iloc[i-1] if i > 0 else d25c
        d30p = d30_day.iloc[i-1] if i > 0 else d30c

        sig25 = _signal_label(d25c, d25p, pos25 > 0)
        sig30 = _signal_label(d30c, d30p, pos30 > 0)

        # 포지션 추적
        if sig25 == "BUY"  and pos25 == 0: pos25 = 1; buy_price25 = close
        if sig25 == "SELL" and pos25 > 0:  pos25 = 0
        if sig30 == "BUY"  and pos30 == 0: pos30 = 1; buy_price30 = close
        if sig30 == "SELL" and pos30 > 0:  pos30 = 0

        # 차이 표시
        diff = ""
        if sig25 != sig30:
            diff = f"← 신호 다름  (2.5={sig25} / 3.0={sig30})"
        if sig25 in ("BUY", "SELL"):
            diff += f"  ★2.5 {sig25}"
        if sig30 in ("BUY", "SELL"):
            diff += f"  ★3.0 {sig30}"

        # 중요 신호만 강조
        highlight = sig25 in ("BUY","SELL") or sig30 in ("BUY","SELL") or sig25 != sig30
        if highlight:
            print(f"▶ {t_str}  {close:>8,.0f}  {s25:>10,.0f}  {sig25:>5}  |  {s30:>10,.0f}  {sig30:>5}  | {diff}")

    print("-" * 80)

    # ── 요약 ──────────────────────────────────────────────────────────────────
    sells25 = [(d25_day.index[i], day_df.iloc[i]["close"])
               for i in range(1, len(d25_day))
               if d25_day.iloc[i-1] == 1 and d25_day.iloc[i] == -1]
    sells30 = [(d30_day.index[i], day_df.iloc[i]["close"])
               for i in range(1, len(d30_day))
               if d30_day.iloc[i-1] == 1 and d30_day.iloc[i] == -1]

    buys25 = [(d25_day.index[i], day_df.iloc[i]["close"])
              for i in range(1, len(d25_day))
              if d25_day.iloc[i-1] == -1 and d25_day.iloc[i] == 1]
    buys30 = [(d30_day.index[i], day_df.iloc[i]["close"])
              for i in range(1, len(d30_day))
              if d30_day.iloc[i-1] == -1 and d30_day.iloc[i] == 1]

    print(f"\n[요약]")
    fmt = lambda items: [t.strftime("%H:%M") + f" @{p:,.0f}" for t, p in items]
    print(f"  m=2.5  BUY:  {fmt(buys25)}")
    print(f"  m=2.5  SELL: {fmt(sells25)}")
    print(f"  m=3.0  BUY:  {fmt(buys30)}")
    print(f"  m=3.0  SELL: {fmt(sells30)}")

    if sells25 and sells30:
        t25 = sells25[0][0]; p25 = sells25[0][1]
        t30 = sells30[0][0]; p30 = sells30[0][1]
        diff_min = int((t30 - t25).total_seconds() / 60) if t30 > t25 else int((t25 - t30).total_seconds() / 60)
        faster = "m=2.5" if t25 < t30 else "m=3.0"
        print(f"\n  → {faster}가 첫 SELL을 {diff_min}분 빠르게 인식")
    elif sells25:
        print(f"\n  → m=2.5만 SELL 인식 (m=3.0는 미인식)")
    elif sells30:
        print(f"\n  → m=3.0만 SELL 인식 (m=2.5는 미인식)")
    else:
        print(f"\n  → 해당 날짜에 SELL 전환 없음 (상승추세 유지)")

    print(f"{'='*80}\n")

    # ── matplotlib 차트 (설치된 경우) ─────────────────────────────────────────
    try:
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        fig, ax = plt.subplots(figsize=(14, 6))
        times = day_df.index

        ax.plot(times, day_df["close"], color="black", linewidth=1.2, label="종가")
        ax.plot(times, st25_day.values, color="blue",  linewidth=1.5, linestyle="--", label="ST m=2.5")
        ax.plot(times, st30_day.values, color="red",   linewidth=1.5, linestyle="--", label="ST m=3.0")

        # BUY/SELL 마커
        for t, p in buys25:
            ax.annotate("B2.5", xy=(t, p), xytext=(0, -20), textcoords="offset points",
                        color="blue", fontsize=8, arrowprops=dict(arrowstyle="->", color="blue"))
        for t, p in sells25:
            ax.annotate("S2.5", xy=(t, p), xytext=(0, 15), textcoords="offset points",
                        color="blue", fontsize=8, arrowprops=dict(arrowstyle="->", color="blue"))
        for t, p in buys30:
            ax.annotate("B3.0", xy=(t, p), xytext=(0, -35), textcoords="offset points",
                        color="red", fontsize=8, arrowprops=dict(arrowstyle="->", color="red"))
        for t, p in sells30:
            ax.annotate("S3.0", xy=(t, p), xytext=(0, 30), textcoords="offset points",
                        color="red", fontsize=8, arrowprops=dict(arrowstyle="->", color="red"))

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 30]))
        plt.xticks(rotation=45)
        ax.set_title(f"005930.KS  {target_date}  Supertrend p=7 비교 (m=2.5 파랑 / m=3.0 빨강)")
        ax.set_ylabel("가격 (원)")
        ax.legend()
        ax.grid(alpha=0.3)
        plt.tight_layout()
        plt.show()
        print("차트 표시 완료")
    except Exception as e:
        print(f"(차트 생략: {e})")


if __name__ == "__main__":
    main()
