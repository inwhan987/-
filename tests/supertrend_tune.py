"""Supertrend 파라미터 튜닝 백테스트.

사용:
  python tests/supertrend_tune.py
  python tests/supertrend_tune.py 005930.KS 60d 5m

period:     [5, 7, 10, 14, 21]
multiplier: [1.0, 1.5, 2.0, 2.5, 3.0]
→ 총 25가지 조합 비교
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from stock_bot.backtest.engine import run_strategy
from stock_bot.backtest.strategies import strategy_supertrend


def _download(symbol: str, period: str, interval: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance 미설치. pip install yfinance")
        sys.exit(1)
    df = yf.download(symbol, period=period, interval=interval,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"데이터 없음: {symbol}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    return df


def main() -> None:
    symbol   = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period   = sys.argv[2] if len(sys.argv) > 2 else "60d"
    interval = sys.argv[3] if len(sys.argv) > 3 else "5m"

    print(f"\n데이터 다운로드: {symbol} ({interval}, {period})...", flush=True)
    df = _download(symbol, period, interval)
    print(f"봉 수: {len(df)}  ({df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')})\n")

    periods      = [5, 7, 10, 14, 21]
    multipliers  = [1.0, 1.5, 2.0, 2.5, 3.0]

    results = []
    for p in periods:
        for m in multipliers:
            def _fn(df, pos, avg, sl, _p=p, _m=m):
                return strategy_supertrend(df, pos, avg, sl, period=_p, multiplier=_m)
            label = f"ST p={p:2d} m={m:.1f}"
            try:
                r = run_strategy(df, _fn, label, initial_cash=10_000_000, stop_loss_pct=5.0)
                results.append(r)
                print(f"  {label} → 수익 {r.total_return_pct:+6.2f}%  거래 {r.trades:3d}회  승률 {r.win_rate:5.1f}%  샤프 {r.sharpe:6.2f}")
            except Exception as exc:
                print(f"  {label} → 오류: {exc}")

    # ── 결과 테이블 ──────────────────────────────────────────────────
    results.sort(key=lambda r: r.sharpe if r.trades >= 3 else -999, reverse=True)

    hdr = f"\n{'파라미터':<16} {'수익률':>8} {'거래':>5} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7} {'평균보유(봉)':>12}"
    print("\n" + "=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(results):
        pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
        mark = "★ " if i == 0 else "  "
        print(
            f"{mark}{r.strategy:<14} "
            f"{r.total_return_pct:>+8.2f}% "
            f"{r.trades:>5} "
            f"{r.win_rate:>6.1f}% "
            f"{r.max_drawdown_pct:>6.1f}% "
            f"{r.sharpe:>7.2f} "
            f"{pf:>7} "
            f"{r.avg_hold_bars:>12.0f}"
        )
    print("=" * len(hdr))

    print(f"\n[최적] {results[0].strategy}  →  수익 {results[0].total_return_pct:+.2f}%  샤프 {results[0].sharpe:.2f}")
    print(f"현재 설정: p=7 m=3.0")


if __name__ == "__main__":
    main()
