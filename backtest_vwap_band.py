"""VWAP 매수/매도 밴드 조합 백테스트.

사용:
  python backtest_vwap_band.py [symbol] [period]
  예) python backtest_vwap_band.py 005930.KS 60d
"""
from __future__ import annotations

import sys
import pandas as pd

from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv

ATR_STOP_MAX_PCT = 5.0


def _make_fn(buy_band: float, sell_band: float, st_bull_sell_band: float):
    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        cfg = EnsembleConfig()
        cfg.vwap_band                   = buy_band
        cfg.vwap_sell_band              = sell_band
        cfg.vwap_st_bull_sell_band      = st_bull_sell_band
        cfg.vwap_warmup_bars            = 8
        cfg.rsi_period                  = 25
        cfg.rsi_oversold                = 30.0
        cfg.rsi_overbought              = 74.0
        cfg.supertrend_period           = 7
        cfg.supertrend_mult             = 2.5
        cfg.bb_window                   = 20
        cfg.bb_k                        = 2.0
        cfg.bb_consec                   = 3
        cfg.weights                     = (0.25, 0.22, 0.20, 0.18, 0.15)
        cfg.min_buy_votes               = 2
        cfg.buy_threshold               = 0.4
        cfg.add_buy_threshold           = 0.45
        cfg.add_buy_min_votes           = 2
        cfg.min_sell_votes              = 2
        cfg.sell_threshold              = -0.3
        cfg.volume_filter_enabled       = True
        cfg.volume_high_ratio           = 1.2
        cfg.volume_low_ratio            = 0.7
        cfg.volume_score_boost          = 0.10
        cfg.volume_score_penalty        = 0.05
        if ctx:
            cfg.daily_context_entry_date      = ctx.get("entry_date")
            cfg.daily_context_prev_day_high   = ctx.get("prev_day_high", 0.0)
            cfg.daily_context_prev_day_close  = ctx.get("prev_day_close", 0.0)

        last_price = float(df_slice["close"].iloc[-1])
        ohlcv_list = [
            {"open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume}
            for r in df_slice.itertuples()
        ]
        atr_val = atr_from_ohlcv(ohlcv_list, period=14)
        stop_pct = min((atr_val * 12.0) / last_price * 100, ATR_STOP_MAX_PCT) if atr_val > 0 else ATR_STOP_MAX_PCT

        today_date = df_slice.index[-1].date()
        df_today = df_slice[df_slice.index.date == today_date]
        decision = decide_ensemble(
            df_slice["close"],
            ohlcv_df=df_today,
            ohlcv_df_hist=df_slice,
            position_qty=position_qty,
            avg_price=avg_price,
            stop_loss_pct=stop_pct,
            config=cfg,
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


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"

    print(f"\n종목: {symbol}  기간: {period}")
    print(f"VWAP 매수/매도 밴드 조합 테스트\n")

    print(f"데이터 다운로드...", end=" ", flush=True)
    df = _download(symbol, period)
    print(f"{len(df)}봉\n")

    # 매수 0.70~0.80%, 매도 매수 이상, ST상승 매도+0.05%
    buy_bands  = [0.0070, 0.0075, 0.0080]
    sell_offsets = [0.0000, 0.0005, 0.0010, 0.0015, 0.0020]  # 매도 = 매수 + offset

    cases = []
    for bb in buy_bands:
        for offset in sell_offsets:
            sb = bb + offset
            st_bull_sb = sb + 0.0005
            cases.append((bb, sb, st_bull_sb))

    hdr = f"{'케이스':<32} {'수익률':>8} {'거래':>5} {'승률':>7} {'MDD':>7} {'샤프':>6} {'손익비':>6}"
    sep = "=" * len(hdr)
    print(sep)
    print(hdr)
    print("-" * len(hdr))

    results = []
    for buy_band, sell_band, st_bull_sb in cases:
        label = f"매수{buy_band*100:.2f}% 매도{sell_band*100:.2f}% ST↑{st_bull_sb*100:.2f}%"
        fn = _make_fn(buy_band, sell_band, st_bull_sb)
        r = run_strategy(df, fn, symbol, stop_loss_pct=ATR_STOP_MAX_PCT)
        pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
        print(
            f"{label:<32} "
            f"{r.total_return_pct:>+8.2f}% "
            f"{r.trades:>5} "
            f"{r.win_rate:>6.1f}% "
            f"{r.max_drawdown_pct:>6.1f}% "
            f"{r.sharpe:>6.2f} "
            f"{pf:>6}"
        )
        results.append((label, r))

    print(sep)
    best = max(results, key=lambda x: x[1].sharpe)
    print(f"▶ 샤프 최고: {best[0]}  ({best[1].sharpe:.2f})")
    best_r = max(results, key=lambda x: x[1].total_return_pct)
    print(f"▶ 수익률 최고: {best_r[0]}  ({best_r[1].total_return_pct:+.2f}%)")
    best_w = max(results, key=lambda x: x[1].win_rate)
    print(f"▶ 승률 최고: {best_w[0]}  ({best_w[1].win_rate:.1f}%)")
    print(sep)


if __name__ == "__main__":
    main()
