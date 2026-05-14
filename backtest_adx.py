"""ADX 기반 VWAP 동적 비활성화 백테스트.

A: 현재 (VWAP 항상 ON)
B: ADX < 20 일 때만 VWAP ON
C: ADX < 25 일 때만 VWAP ON
D: ADX < 30 일 때만 VWAP ON

사용:
  python backtest_adx.py [symbol1,symbol2,...] [period]
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd

from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv

ATR_CAP = 5.0
# 기본 가중치
W_VWAP, W_ST, W_RSI, W_BB, W_DC = 0.28, 0.24, 0.16, 0.12, 0.20
# VWAP 없을 때 나머지에 비율 배분
_REST = W_ST + W_RSI + W_BB + W_DC  # 0.72
W_NO_VWAP = (
    0.0,
    round(W_ST  / _REST, 4),
    round(W_RSI / _REST, 4),
    round(W_BB  / _REST, 4),
    round(W_DC  / _REST, 4),
)  # (0, 0.333, 0.222, 0.167, 0.278)


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX 계산 (EWM 방식)."""
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    close = df["close"].values.astype(float)
    n = len(df)

    tr  = np.empty(n); tr[0]  = high[0] - low[0]
    pdm = np.empty(n); pdm[0] = 0.0
    ndm = np.empty(n); ndm[0] = 0.0

    for i in range(1, n):
        h_diff = high[i]  - high[i-1]
        l_diff = low[i-1] - low[i]
        tr[i]  = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
        pdm[i] = h_diff if h_diff > l_diff and h_diff > 0 else 0.0
        ndm[i] = l_diff if l_diff > h_diff and l_diff > 0 else 0.0

    alpha = 1.0 / period
    atr_s = np.empty(n); atr_s[0] = tr[0]
    pdi_s = np.empty(n); pdi_s[0] = pdm[0]
    ndi_s = np.empty(n); ndi_s[0] = ndm[0]

    for i in range(1, n):
        atr_s[i] = alpha * tr[i]  + (1-alpha) * atr_s[i-1]
        pdi_s[i] = alpha * pdm[i] + (1-alpha) * pdi_s[i-1]
        ndi_s[i] = alpha * ndm[i] + (1-alpha) * ndi_s[i-1]

    pdi = np.where(atr_s > 0, pdi_s / atr_s * 100, 0.0)
    ndi = np.where(atr_s > 0, ndi_s / atr_s * 100, 0.0)
    dx  = np.where((pdi + ndi) > 0, np.abs(pdi - ndi) / (pdi + ndi) * 100, 0.0)

    adx = np.empty(n); adx[0] = dx[0]
    for i in range(1, n):
        adx[i] = alpha * dx[i] + (1-alpha) * adx[i-1]

    return pd.Series(adx, index=df.index)


def _make(adx_arr: np.ndarray | None, adx_threshold: float | None):
    """adx_arr: 전체 df에서 미리 계산된 ADX 배열 (len = len(df)).
    adx_threshold=None 이면 항상 VWAP ON."""
    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        cfg = EnsembleConfig()
        cfg.vwap_warmup_bars            = 8
        cfg.rsi_period                  = 25
        cfg.rsi_oversold                = 30.0
        cfg.rsi_overbought              = 74.0
        cfg.supertrend_period           = 7
        cfg.supertrend_mult             = 2.5
        cfg.bb_window                   = 20
        cfg.bb_k                        = 2.0
        cfg.bb_consec                   = 3
        cfg.min_buy_votes               = 2
        cfg.buy_threshold               = 0.4
        cfg.min_sell_votes              = 2
        cfg.sell_threshold              = -0.3
        cfg.volume_filter_enabled       = True
        cfg.volume_high_ratio           = 1.2
        cfg.volume_low_ratio            = 0.7
        cfg.volume_score_boost          = 0.10
        cfg.volume_score_penalty        = 0.05
        cfg.daily_context_profit_gate_pct = 1.5
        cfg.daily_context_avwap_pct     = 1.5
        cfg.daily_context_pdh_pct       = 1.0
        cfg.daily_context_pdc_pct       = 1.5
        if ctx:
            cfg.daily_context_entry_date     = ctx.get("entry_date")
            cfg.daily_context_prev_day_high  = ctx.get("prev_day_high", 0.0)
            cfg.daily_context_prev_day_close = ctx.get("prev_day_close", 0.0)

        # ADX 룩업 (현재 봉 인덱스 = len(df_slice) - 1)
        if adx_arr is not None and adx_threshold is not None:
            idx = len(df_slice) - 1
            current_adx = float(adx_arr[idx]) if idx < len(adx_arr) else 0.0
            vwap_on = current_adx < adx_threshold
        else:
            vwap_on = True

        if vwap_on:
            cfg.vwap_band  = 0.0085
            cfg.weights    = (W_VWAP, W_ST, W_RSI, W_BB, W_DC)
        else:
            cfg.vwap_band  = 9999.0
            cfg.weights    = W_NO_VWAP

        # ATR (최근 50봉만)
        last_price = float(df_slice["close"].iloc[-1])
        ohlcv_list = [
            {"open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume}
            for r in df_slice.iloc[-50:].itertuples()
        ]
        atr_val = atr_from_ohlcv(ohlcv_list, period=14)
        stop_pct = min((atr_val * 12.0) / last_price * 100, ATR_CAP) if atr_val > 0 and last_price > 0 else ATR_CAP

        decision = decide_ensemble(
            df_slice["close"], df_slice,
            position_qty, avg_price, stop_pct, cfg,
        )
        return decision.signal.value
    return _fn


def _download(symbol: str, period: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(symbol, period=period, interval="5m",
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"데이터 없음: {symbol}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    return df


ADX_CASES = [
    ("A VWAP항상ON", None),
    ("B ADX<20",     20),
    ("C ADX<25",     25),
    ("D ADX<30",     30),
]


def main():
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    print(f"\n기간: {period}  종목: {', '.join(symbols)}")
    print("데이터 다운로드 중...", flush=True)
    dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            dfs[sym] = _download(sym, period)
            print(f"  {sym}: {len(dfs[sym])}봉")
        except Exception as e:
            print(f"  {sym}: 오류 - {e}")

    # ADX 미리 계산 (종목별, 1회)
    import warnings
    adx_arrays: dict[str, np.ndarray] = {}
    for sym, df in dfs.items():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            adx_arrays[sym] = _adx(df, period=14).values

    hdr = f"{'케이스':<16} {'종목':<12} {'수익률':>8} {'거래':>5} {'승률':>7} {'MDD':>7} {'샤프':>7}"
    sep = "=" * len(hdr)

    case_totals: dict[str, list[float]] = {label: [] for label, _ in ADX_CASES}

    for label, threshold in ADX_CASES:
        print(f"\n{sep}\n▶ {label}\n{'-'*len(hdr)}")
        for sym, df in dfs.items():
            try:
                adx_arr = adx_arrays[sym] if threshold is not None else None
                fn = _make(adx_arr, threshold)
                r = run_strategy(df, fn, sym, stop_loss_pct=ATR_CAP)
                print(
                    f"{label:<16} {sym:<12} "
                    f"{r.total_return_pct:>+8.2f}% "
                    f"{r.trades:>5} "
                    f"{r.win_rate:>6.1f}% "
                    f"{r.max_drawdown_pct:>6.1f}% "
                    f"{r.sharpe:>7.2f}"
                )
                case_totals[label].append(r.total_return_pct)
            except Exception as e:
                print(f"{label:<16} {sym:<12} 오류: {e}")

    print(f"\n{sep}\n▶ 케이스별 평균 수익률\n{'-'*45}")
    for label, returns in case_totals.items():
        if returns:
            avg   = sum(returns) / len(returns)
            best  = max(returns)
            worst = min(returns)
            wins  = sum(1 for r in returns if r > 0)
            print(f"  {label:<16} 평균 {avg:>+7.2f}%  최고 {best:>+7.2f}%  최저 {worst:>+7.2f}%  흑자종목 {wins}/{len(returns)}")
    print(sep)


if __name__ == "__main__":
    main()
