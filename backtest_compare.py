"""개선안 비교 백테스트.

A: 현재 설정 (baseline)
B: 임계값 상향 (buy 0.5 / sell -0.45)
C: 손절 캡 축소 (3.5%)
D: A+B+C 조합

사용:
  python backtest_compare.py [symbol1,symbol2,...] [period]
"""
from __future__ import annotations

import sys
import pandas as pd

from stock_bot.backtest.engine import run_strategy, BacktestResult
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv


def _make(
    buy_threshold: float = 0.4,
    sell_threshold: float = -0.3,
    min_buy_votes: int = 2,
    min_sell_votes: int = 2,
    atr_cap: float = 5.0,
):
    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        cfg = EnsembleConfig()
        cfg.vwap_band                   = 0.0085
        cfg.vwap_warmup_bars            = 8
        cfg.rsi_period                  = 25
        cfg.rsi_oversold                = 30.0
        cfg.rsi_overbought              = 74.0
        cfg.supertrend_period           = 7
        cfg.supertrend_mult             = 2.5
        cfg.bb_window                   = 20
        cfg.bb_k                        = 2.0
        cfg.bb_consec                   = 3
        cfg.weights                     = (0.28, 0.24, 0.16, 0.12, 0.20)
        cfg.min_buy_votes               = min_buy_votes
        cfg.buy_threshold               = buy_threshold
        cfg.min_sell_votes              = min_sell_votes
        cfg.sell_threshold              = sell_threshold
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

        last_price = float(df_slice["close"].iloc[-1])
        ohlcv_list = [
            {"open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume}
            for r in df_slice.itertuples()
        ]
        atr_val = atr_from_ohlcv(ohlcv_list, period=14)
        if atr_val > 0 and last_price > 0:
            stop_pct = min((atr_val * 12.0) / last_price * 100, atr_cap)
        else:
            stop_pct = atr_cap

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


CASES = [
    ("A 현재설정",           _make(0.40, -0.30, 2, 2, 5.0)),
    ("B 임계값상향",         _make(0.50, -0.45, 2, 2, 5.0)),
    ("C 손절캡3.5%",         _make(0.40, -0.30, 2, 2, 3.5)),
    ("D A+B+C 조합",         _make(0.50, -0.45, 2, 2, 3.5)),
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

    hdr = f"{'케이스':<18} {'종목':<12} {'수익률':>8} {'거래':>5} {'승률':>7} {'MDD':>7} {'샤프':>7}"
    sep = "=" * len(hdr)

    # 케이스별 평균 수익률 집계
    case_totals: dict[str, list[float]] = {label: [] for label, _ in CASES}

    for label, fn in CASES:
        print(f"\n{sep}")
        print(f"▶ {label}")
        print("-" * len(hdr))
        for sym, df in dfs.items():
            cap = 5.0 if "3.5" not in label else 3.5
            try:
                r = run_strategy(df, fn, sym, stop_loss_pct=cap)
                pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
                print(
                    f"{label:<18} {sym:<12} "
                    f"{r.total_return_pct:>+8.2f}% "
                    f"{r.trades:>5} "
                    f"{r.win_rate:>6.1f}% "
                    f"{r.max_drawdown_pct:>6.1f}% "
                    f"{r.sharpe:>7.2f}"
                )
                case_totals[label].append(r.total_return_pct)
            except Exception as e:
                print(f"{label:<18} {sym:<12} 오류: {e}")

    print(f"\n{sep}")
    print(f"▶ 케이스별 평균 수익률 비교")
    print("-" * 40)
    for label, returns in case_totals.items():
        if returns:
            avg = sum(returns) / len(returns)
            best = max(returns)
            worst = min(returns)
            print(f"  {label:<18} 평균 {avg:>+7.2f}%  최고 {best:>+7.2f}%  최저 {worst:>+7.2f}%")
    print(sep)


if __name__ == "__main__":
    main()
