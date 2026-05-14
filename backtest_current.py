"""현재 적용 설정 기준 백테스트.

사용:
  python backtest_current.py [symbol1,symbol2,...] [period]
  예) python backtest_current.py 005930.KS,035720.KS,000660.KS 60d
"""
from __future__ import annotations

import sys
import pandas as pd

from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv

ATR_STOP_MAX_PCT = 5.0


def _make_current():
    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        cfg = EnsembleConfig()
        cfg.vwap_band                   = 0.0065
        cfg.vwap_sell_band              = 0.007
        cfg.vwap_st_bull_sell_band      = 0.0075
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
        cfg.daily_context_profit_gate_pct = 1.5
        cfg.daily_context_avwap_pct     = 1.5
        cfg.daily_context_pdh_pct       = 1.0
        cfg.daily_context_pdc_pct       = 1.5

        # ctx에서 DailyContext 정보 주입
        if ctx:
            cfg.daily_context_entry_date      = ctx.get("entry_date")
            cfg.daily_context_prev_day_high   = ctx.get("prev_day_high", 0.0)
            cfg.daily_context_prev_day_close  = ctx.get("prev_day_close", 0.0)

        # ATR 동적 손절 (캡 5%)
        last_price = float(df_slice["close"].iloc[-1])
        ohlcv_list = [
            {"open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume}
            for r in df_slice.itertuples()
        ]
        atr_val = atr_from_ohlcv(ohlcv_list, period=14)
        if atr_val > 0 and last_price > 0:
            dynamic_pct = (atr_val * 12.0) / last_price * 100
            stop_pct = min(dynamic_pct, ATR_STOP_MAX_PCT)
        else:
            stop_pct = ATR_STOP_MAX_PCT

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
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    print(f"\n기간: {period}  전략: 현재 앙상블 (VWAP매수0.65%/매도0.70%/ST상승0.75%/RSI25/ST7×2.5/ATR캡5%)")
    print(f"종목: {', '.join(symbols)}\n")

    hdr = f"{'종목':<14} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7}"
    sep = "=" * len(hdr)
    print(sep)
    print(hdr)
    print("-" * len(hdr))

    fn = _make_current()
    total_returns = []

    for symbol in symbols:
        try:
            print(f"  {symbol} 다운로드 중...", end=" ", flush=True)
            df = _download(symbol, period)
            print(f"{len(df)}봉", flush=True)
            r = run_strategy(df, fn, symbol, stop_loss_pct=ATR_STOP_MAX_PCT)
            pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
            print(
                f"{symbol:<14} "
                f"{r.total_return_pct:>+8.2f}% "
                f"{r.trades:>6} "
                f"{r.win_rate:>6.1f}% "
                f"{r.max_drawdown_pct:>6.1f}% "
                f"{r.sharpe:>7.2f} "
                f"{pf:>7}"
            )
            total_returns.append(r.total_return_pct)
        except Exception as e:
            print(f"{symbol:<14} 오류: {e}")

    if total_returns:
        avg = sum(total_returns) / len(total_returns)
        print(sep)
        print(f"{'평균':>14} {avg:>+8.2f}%")
        print(sep)


if __name__ == "__main__":
    main()
