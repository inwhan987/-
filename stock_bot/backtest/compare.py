"""전략 비교 백테스트 실행기.

사용:
  python main.py backtest compare [symbol] [--interval 5m] [--period 60d] [--cash 10000000]

데이터: yfinance (국내 종목은 .KS 접미사 필요, e.g. 005930.KS)
"""
from __future__ import annotations

import sys
from typing import Any

import pandas as pd

from .engine import BacktestResult, run_strategy
from .strategies import STRATEGIES


def _download(symbol: str, period: str, interval: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance 미설치. pip install yfinance 후 재시도.")
        sys.exit(1)

    df = yf.download(symbol, period=period, interval=interval,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"데이터 없음: {symbol}")

    # 컬럼 소문자화
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]

    # 필요 컬럼 확인
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            raise ValueError(f"컬럼 없음: {col} (실제 컬럼: {list(df.columns)})")

    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    return df


def run_compare(
    symbol: str = "005930.KS",
    period: str = "60d",
    interval: str = "5m",
    cash: float = 10_000_000,
    stop_loss_pct: float = 5.0,
    strategy_keys: list[str] | None = None,
) -> list[BacktestResult]:
    print(f"\n데이터 다운로드: {symbol} ({interval}, {period})...", flush=True)
    df = _download(symbol, period, interval)
    print(f"  봉 수: {len(df)}  ({df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')})\n")

    # ensemble_dc 를 기본 비교 목록의 맨 앞에 포함
    _default_keys = ["ensemble_dc"] + [k for k in STRATEGIES.keys() if k != "ensemble_dc"]
    keys = strategy_keys or _default_keys
    results: list[BacktestResult] = []

    for key in keys:
        fn, label = STRATEGIES[key]
        print(f"  [{key:12s}] {label} ...", end=" ", flush=True)
        try:
            r = run_strategy(df, fn, label, initial_cash=cash, stop_loss_pct=stop_loss_pct)
            results.append(r)
            print(f"수익 {r.total_return_pct:+.2f}%  거래 {r.trades}회")
        except Exception as exc:
            print(f"오류: {exc}")

    return results


def print_table(results: list[BacktestResult], cash: float = 10_000_000) -> None:
    if not results:
        print("결과 없음")
        return

    # 샤프 기준 정렬 (음수 샤프면 수익률 기준)
    results = sorted(results, key=lambda r: (r.sharpe if r.trades > 2 else -999), reverse=True)

    hdr = f"{'전략':<32} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7} {'평균보유(봉)':>12}"
    print("\n" + "=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))

    for r in results:
        pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
        mark = "★ " if results.index(r) == 0 else "  "
        print(
            f"{mark}{r.strategy:<30} "
            f"{r.total_return_pct:>+8.2f}% "
            f"{r.trades:>6} "
            f"{r.win_rate:>6.1f}% "
            f"{r.max_drawdown_pct:>6.1f}% "
            f"{r.sharpe:>7.2f} "
            f"{pf:>7} "
            f"{r.avg_hold_bars:>12.0f}"
        )

    print("=" * len(hdr))

    # 앙상블 재구성 추천 (거래 3회 이상, 상위 4개)
    ranked = [r for r in results if r.trades >= 3][:6]
    if ranked:
        print("\n[추천] 앙상블 구성 후보 (샤프 상위):")
        for i, r in enumerate(ranked[:4], 1):
            print(f"  {i}. {r.strategy}  (수익 {r.total_return_pct:+.2f}%, 샤프 {r.sharpe:.2f})")
