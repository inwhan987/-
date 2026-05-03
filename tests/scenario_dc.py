"""시나리오 테스트: DailyContext + Supertrend 신호 시뮬레이션.

시나리오:
  - Day 1 (2026-04-27): 삼성전자(005930) 223,000에 8주 매수
  - Day 2 (2026-04-28): 장 초 223,500 시작 → 봉 40 부근 228,000 고점 → 224,000 하락
  - Day 2 매 봉마다 DailyContext · Supertrend 신호를 출력

실행:
  python tests/scenario_dc.py
"""
from __future__ import annotations

import sys
import os

# 프로젝트 루트를 sys.path 에 추가 (python tests/scenario_dc.py 방식 지원)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from stock_bot.strategy.daily_context import decide_daily_context
from stock_bot.strategy.supertrend import decide_supertrend

# ── 파라미터 ──────────────────────────────────────────────────────────────────
POSITION_QTY = 8
AVG_PRICE = 223_000.0
ENTRY_DATE = "2026-04-27"

PREV_DAY_HIGH = 224_500.0
PREV_DAY_CLOSE = 223_000.0

DC_PARAMS = dict(
    entry_date=ENTRY_DATE,
    prev_day_high=PREV_DAY_HIGH,
    prev_day_close=PREV_DAY_CLOSE,
    profit_gate_pct=1.5,
    avwap_pct=1.5,
    pdh_pct=1.0,
    pdc_pct=1.5,
)

# ── 합성 데이터 생성 ──────────────────────────────────────────────────────────

def _make_bars(start_dt: datetime, prices: list[float], volume: int = 500_000) -> pd.DataFrame:
    """종가 리스트 → 5분봉 DataFrame (open=prev_close, high/low=±0.1% 노이즈)."""
    rng = np.random.default_rng(42)
    rows = []
    for i, close in enumerate(prices):
        open_ = prices[i - 1] if i > 0 else close
        noise = close * 0.001
        high = close + abs(rng.normal(0, noise))
        low  = close - abs(rng.normal(0, noise))
        # open 이 범위를 벗어나지 않게 클리핑
        open_ = float(np.clip(open_, low, high))
        rows.append({
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume + rng.integers(-50_000, 50_000),
        })
    times = [start_dt + timedelta(minutes=5 * i) for i in range(len(prices))]
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(times))
    return df


def make_day1() -> pd.DataFrame:
    """Day 1: 09:00~15:30 KST, 78봉, 223,000 부근 횡보."""
    start = datetime(2026, 4, 27, 9, 0)
    rng = np.random.default_rng(1)
    prices = [223_000 + rng.integers(-200, 200) for _ in range(78)]
    # 마지막 봉 정확히 223,000 으로 설정 (prev_day_close)
    prices[-1] = 223_000
    return _make_bars(start, prices)


def make_day2() -> pd.DataFrame:
    """Day 2: 09:00~15:30 KST, 78봉.

    0~39봉: 223,500 → 228,000 선형 상승
    40~77봉: 228,000 → 224,000 선형 하락
    """
    start = datetime(2026, 4, 28, 9, 0)
    rise  = np.linspace(223_500, 228_000, 40).tolist()
    fall  = np.linspace(228_000, 224_000, 38).tolist()
    prices = rise + fall
    return _make_bars(start, [round(p) for p in prices])


# ── 메인 시뮬레이션 ───────────────────────────────────────────────────────────

def main() -> None:
    df_day1 = make_day1()
    df_day2 = make_day2()

    # Day 1 + Day 2 전체 DataFrame (Supertrend warmup 용)
    df_all = pd.concat([df_day1, df_day2])

    print("=" * 76)
    print("시나리오: 005930 삼성전자 - DailyContext & Supertrend 신호 시뮬레이션")
    print(f"  매수 조건: {POSITION_QTY}주, 평단 {AVG_PRICE:,.0f}원, 진입일 {ENTRY_DATE}")
    print(f"  전일 고가: {PREV_DAY_HIGH:,.0f}  전일 종가: {PREV_DAY_CLOSE:,.0f}")
    print("=" * 76)
    print(f"{'봉':>3}  {'시각':^18}  {'종가':>9}  {'수익률':>7}  {'DailyContext':^16}  {'Supertrend':^16}  {'알림':^10}")
    print("-" * 100)

    day2_offset = len(df_day1)   # Day 2 첫 봉의 전체 인덱스
    first_dc_sell   = None
    first_st_sell   = None
    first_both_sell = None

    for bar_idx in range(len(df_day2)):
        # 현재까지의 전체 슬라이스 (Day 1 + Day 2 봉 0..bar_idx)
        global_end = day2_offset + bar_idx + 1
        df_slice = df_all.iloc[:global_end]

        bar_time = df_day2.index[bar_idx]
        price    = float(df_day2["close"].iloc[bar_idx])
        profit   = (price - AVG_PRICE) / AVG_PRICE * 100

        # ── DailyContext ──────────────────────────────────────────────────
        dc = decide_daily_context(
            ohlcv_df=df_slice,
            position_qty=POSITION_QTY,
            avg_price=AVG_PRICE,
            **DC_PARAMS,
        )
        dc_sig = dc.signal.value   # "buy" / "sell" / "hold"

        # ── Supertrend ────────────────────────────────────────────────────
        st = decide_supertrend(
            df=df_slice,
            period=7,
            multiplier=3.0,
            position_qty=POSITION_QTY,
            avg_price=AVG_PRICE,
            stop_loss_pct=5.0,
        )
        st_sig = st.signal.value

        # ── 첫 SELL 감지 ──────────────────────────────────────────────────
        note = ""
        if dc_sig == "sell" and first_dc_sell is None:
            first_dc_sell = bar_idx
            note += "★DC첫SELL "
        if st_sig == "sell" and first_st_sell is None:
            first_st_sell = bar_idx
            note += "★ST첫SELL "
        if dc_sig == "sell" and st_sig == "sell" and first_both_sell is None:
            first_both_sell = bar_idx
            note += "★양쪽SELL"

        # 컬러 표시: SELL 이면 "[SELL]", HOLD 이면 "hold"
        def fmt(sig: str) -> str:
            return f"[{sig.upper():^6}]" if sig == "sell" else f" {sig:^6} "

        time_str = bar_time.strftime("%m-%d %H:%M")
        print(
            f"{bar_idx:>3}  {time_str:^18}  {price:>9,.0f}  {profit:>+6.2f}%"
            f"  {fmt(dc_sig):^16}  {fmt(st_sig):^16}  {note}"
        )

    print("-" * 100)
    print("\n[요약]")
    if first_dc_sell is not None:
        t = df_day2.index[first_dc_sell].strftime("%H:%M")
        p = float(df_day2["close"].iloc[first_dc_sell])
        print(f"  DailyContext 첫 SELL : 봉 {first_dc_sell:>2} ({t})  가격 {p:,.0f}원")
    else:
        print("  DailyContext SELL 없음")

    if first_st_sell is not None:
        t = df_day2.index[first_st_sell].strftime("%H:%M")
        p = float(df_day2["close"].iloc[first_st_sell])
        print(f"  Supertrend  첫 SELL : 봉 {first_st_sell:>2} ({t})  가격 {p:,.0f}원")
    else:
        print("  Supertrend SELL 없음")

    if first_both_sell is not None:
        t = df_day2.index[first_both_sell].strftime("%H:%M")
        p = float(df_day2["close"].iloc[first_both_sell])
        print(f"  양쪽 동시 SELL      : 봉 {first_both_sell:>2} ({t})  가격 {p:,.0f}원")
    else:
        print("  양쪽 동시 SELL 없음")
    print()


if __name__ == "__main__":
    main()
