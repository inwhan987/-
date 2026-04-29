"""파라미터 튜닝 백테스트 비교.

A: 현재 적용 (vwap=0.007, rsi=30/72, min_sell=2, 5분봉)
B: A + min_sell=3 (4번 제안)
C: B + 10분봉    (4+5번 제안)

사용:
  python backtest_tuning.py [symbol] [period]
  예) python backtest_tuning.py 005930.KS 60d
"""
from __future__ import annotations

import sys
import pandas as pd

from stock_bot.backtest.engine import run_strategy, BacktestResult
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.strategy.ma_cross import MACrossSignal


def _make(vwap_band, rsi_os, rsi_ob, min_sell, st_mult=3.0, bb_k=2.0,
          st_period=7, rsi_period=21, weights=None):
    def _fn(df, position_qty, avg_price, stop_loss_pct):
        cfg = EnsembleConfig()
        cfg.vwap_band          = vwap_band
        cfg.rsi_oversold       = rsi_os
        cfg.rsi_overbought     = rsi_ob
        cfg.min_sell_votes     = min_sell
        cfg.supertrend_mult    = st_mult
        cfg.supertrend_period  = st_period
        cfg.rsi_period         = rsi_period
        cfg.bb_k               = bb_k
        if weights:
            cfg.weights        = weights
        sig = decide_ensemble(df["close"], df, position_qty, avg_price, stop_loss_pct, cfg)
        return sig.signal.value
    return _fn


def _download(symbol: str, period: str, interval: str) -> pd.DataFrame:
    import yfinance as yf
    # yfinance는 10m 미지원 → 5m 다운 후 리샘플링
    fetch_interval = "5m" if interval == "10m" else interval
    df = yf.download(symbol, period=period, interval=fetch_interval,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"데이터 없음: {symbol} {fetch_interval}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    if interval == "10m":
        df = df.resample("10min").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna(subset=["close"])
    return df


def print_table(results: list[tuple[str, BacktestResult]]) -> None:
    hdr = f"{'케이스':<38} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7}"
    print("\n" + "=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for label, r in results:
        pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
        print(
            f"{label:<38} "
            f"{r.total_return_pct:>+8.2f}% "
            f"{r.trades:>6} "
            f"{r.win_rate:>6.1f}% "
            f"{r.max_drawdown_pct:>6.1f}% "
            f"{r.sharpe:>7.2f} "
            f"{pf:>7}"
        )
    print("=" * len(hdr))


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"
    mode   = sys.argv[3] if len(sys.argv) > 3 else "vwap"

    print(f"\n종목: {symbol}  기간: {period}  모드: {mode}")

    if mode == "st_period":
        # Supertrend period 스윕 (현재 RSI=21, BB=15/1.8 반영)
        # label, interval, vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k, st_period, rsi_period
        cases = [
            ("ST period=3  (매우 빠름)", "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 3,  21),
            ("ST period=5  (빠름)",      "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 5,  21),
            ("ST period=7  (현재)",      "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 7,  21),
            ("ST period=9  (느림)",      "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 9,  21),
            ("ST period=10 (매우 느림)", "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 10, 21),
        ]
        results = []
        df_cache: dict[str, pd.DataFrame] = {}
        for label, interval, vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k, st_p, rsi_p in cases:
            if interval not in df_cache:
                print(f"데이터 다운로드 ({interval})...", flush=True)
                df_cache[interval] = _download(symbol, period, interval)
            df = df_cache[interval]
            fn = _make(vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k, st_period=st_p, rsi_period=rsi_p)
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<28} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")
        print_table(results)
        return
    elif mode == "vwap":
        bands = [0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.010, 0.012, 0.015]
        cases = [(f"vwap={b:.3f}", "5m", b, 30.0, 72.0, 2, 3.0, 2.0) for b in bands]
    elif mode == "supertrend":
        cases = [
            ("현재: ST mult=3.0, BB k=2.0", "5m", 0.007, 30.0, 72.0, 2, 3.0, 2.0),
            ("ST mult=2.0, BB k=2.0",        "5m", 0.007, 30.0, 72.0, 2, 2.0, 2.0),
            ("ST mult=2.0, BB k=1.5",        "5m", 0.007, 30.0, 72.0, 2, 2.0, 1.5),
            ("ST mult=3.0, BB k=1.5",        "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.5),
        ]
    elif mode == "fraction":
        # 포지션 비율 스윕 — engine은 항상 95% cash 쓰므로 수익률 변화 없음
        # 대신 실제 금액 기준 수익 추정
        account = 10_000_000
        price   = 58_000
        fracs   = [0.20, 0.30, 0.40, 0.50, 0.60]
        print(f"\n계좌 {account:,}원 / 삼성 {price:,}원 기준 매수금액·주수 시뮬레이션")
        print(f"{'비율':>6}  {'매수금액':>12}  {'주수':>6}  {'1%이익':>10}  {'5%이익':>10}  {'MDD시손실':>12}")
        print("-" * 65)
        for f in fracs:
            budget = account * f
            qty    = int(budget // price)
            gain1  = qty * price * 0.01
            gain5  = qty * price * 0.05
            loss_mdd = qty * price * 0.083  # 백테스트 MDD 8.3% 기준
            print(f"{f*100:>5.0f}%  {budget:>12,.0f}원  {qty:>5}주  {gain1:>9,.0f}원  {gain5:>9,.0f}원  -{loss_mdd:>9,.0f}원")
        print()
        return
    else:
        cases = [
            ("A: 현재(vwap=0.007,rsi=30/72,sell=2,5m)", "5m",  0.007, 30.0, 72.0, 2, 3.0, 2.0),
            ("B: A + min_sell=3 (5m)",                   "5m",  0.007, 30.0, 72.0, 3, 3.0, 2.0),
            ("C: B + 10분봉 (min_sell=3, 10m)",           "10m", 0.007, 30.0, 72.0, 3, 3.0, 2.0),
        ]

    results = []
    df_cache: dict[str, pd.DataFrame] = {}

    for label, interval, vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k in cases:
        if interval not in df_cache:
            print(f"데이터 다운로드 ({interval})...", flush=True)
            df_cache[interval] = _download(symbol, period, interval)
        df = df_cache[interval]

        fn = _make(vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k, rsi_period=21)
        r = run_strategy(df, fn, label)
        results.append((label, r))
        print(f"  {label:<28} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")

    print_table(results)


if __name__ == "__main__":
    main()
