"""KAMA 파라미터 튜닝 백테스트.

사용:
  python backtest_kama_tune.py [symbol1,...] [period]
"""
from __future__ import annotations
import os, sys, tempfile, shutil
from pathlib import Path

try:
    import certifi
    _cert_src = certifi.where()
    _cert_dst = os.path.join(tempfile.gettempdir(), "cacert.pem")
    if not os.path.exists(_cert_dst):
        shutil.copy(_cert_src, _cert_dst)
    os.environ.setdefault("CURL_CA_BUNDLE", _cert_dst)
    os.environ.setdefault("SSL_CERT_FILE", _cert_dst)
except Exception:
    pass

import pandas as pd
from backtest_current import _load_env, ATR_STOP_MAX_PCT
from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv


def _make_kama(kama_period: int, kama_fast: int, kama_slow: int):
    env = _load_env()

    def _g(key, default, cast=float):
        try:
            return cast(env[key]) if key in env else default
        except Exception:
            return default

    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        cfg = EnsembleConfig()
        cfg.vwap_band                   = _g("TRADE_VWAP_BAND",              0.008)
        cfg.vwap_sell_band              = _g("TRADE_VWAP_SELL_BAND",         0.0085) or None
        cfg.vwap_st_bull_sell_band      = _g("TRADE_VWAP_ST_BULL_SELL_BAND", 0.009) or None
        cfg.vwap_warmup_bars            = _g("TRADE_VWAP_WARMUP_BARS",       8, int)
        cfg.rsi_period                  = _g("TRADE_RSI_PERIOD",             25, int)
        cfg.rsi_oversold                = _g("TRADE_RSI_OVERSOLD",           30.0)
        cfg.rsi_overbought              = _g("TRADE_RSI_OVERBOUGHT",         74.0)
        cfg.supertrend_period           = _g("TRADE_SUPERTREND_PERIOD",      7, int)
        cfg.supertrend_mult             = _g("TRADE_SUPERTREND_MULT",        2.5)
        cfg.bb_window                   = _g("TRADE_BB_WINDOW",  20, int)
        cfg.bb_k                        = _g("TRADE_BB_K",       2.0)
        cfg.bb_consec                   = _g("TRADE_BB_CONSEC",  3, int)
        raw_w = env.get("ENSEMBLE_WEIGHTS", "0.25,0.22,0.20,0.18,0.15")
        try:
            cfg.weights = tuple(float(x) for x in raw_w.split(","))
        except Exception:
            cfg.weights = (0.25, 0.22, 0.20, 0.18, 0.15)
        cfg.min_buy_votes               = _g("ENSEMBLE_MIN_BUY_VOTES",       2, int)
        cfg.buy_threshold               = _g("ENSEMBLE_BUY_THRESHOLD",       0.4)
        cfg.add_buy_threshold           = _g("ADD_BUY_THRESHOLD",            0.45)
        cfg.add_buy_min_votes           = _g("ADD_BUY_MIN_VOTES",            2, int)
        cfg.min_sell_votes              = _g("ENSEMBLE_MIN_SELL_VOTES",      2, int)
        cfg.sell_threshold              = _g("ENSEMBLE_SELL_THRESHOLD",      -0.3)
        cfg.volume_filter_enabled       = env.get("ENSEMBLE_VOLUME_FILTER_ENABLED", "true").lower() == "true"
        cfg.volume_ma_period            = _g("ENSEMBLE_VOLUME_MA_PERIOD",    25, int)
        cfg.volume_high_ratio           = _g("ENSEMBLE_VOLUME_HIGH_RATIO",   1.2)
        cfg.volume_low_ratio            = _g("ENSEMBLE_VOLUME_LOW_RATIO",    0.7)
        cfg.volume_score_boost          = _g("ENSEMBLE_VOLUME_SCORE_BOOST",  0.10)
        cfg.volume_score_penalty        = _g("ENSEMBLE_VOLUME_SCORE_PENALTY",0.05)
        cfg.daily_context_profit_gate_pct = _g("DAILY_CONTEXT_PROFIT_GATE_PCT", 1.5)
        cfg.daily_context_avwap_pct       = _g("DAILY_CONTEXT_AVWAP_PCT",       1.5)
        cfg.daily_context_pdh_pct         = _g("DAILY_CONTEXT_PDH_PCT",         1.0)
        cfg.daily_context_pdc_pct         = _g("DAILY_CONTEXT_PDC_PCT",         1.5)
        cfg.daily_context_trend_bonus     = _g("DAILY_CONTEXT_TREND_BONUS",     0.5)
        cfg.ema_trend_enabled             = env.get("ENSEMBLE_EMA_TREND_ENABLED", "false").lower() == "true"
        cfg.ema_trend_weight              = _g("ENSEMBLE_EMA_TREND_WEIGHT",    0.15)
        cfg.ema_trend_fast                = _g("ENSEMBLE_EMA_TREND_FAST",      9, int)
        cfg.ema_trend_slow                = _g("ENSEMBLE_EMA_TREND_SLOW",      21, int)
        # KAMA 활성
        cfg.kama_enabled  = True
        cfg.kama_period   = kama_period
        cfg.kama_fast     = kama_fast
        cfg.kama_slow     = kama_slow
        cfg.kama_weight   = _g("ENSEMBLE_KAMA_WEIGHT", 0.225)

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
        raise ValueError(f"no data: {symbol}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    return df


def _run(df, symbol, fn):
    env = _load_env()
    r = run_strategy(
        df, fn, symbol, stop_loss_pct=ATR_STOP_MAX_PCT,
        enable_add_buy=env.get("ADD_BUY_ENABLED", "true").lower() == "true",
        add_buy_fraction=float(env.get("ADD_BUY_FRACTION", "0.20")),
        add_buy_max_count=int(env.get("ADD_BUY_MAX_COUNT", "2")),
        add_buy_max_position_pct=float(env.get("ADD_BUY_MAX_POSITION_PCT", "0.80")),
        inherit_initial_stop=env.get("ADD_BUY_INHERIT_INITIAL_STOP", "true").lower() == "true",
        post_stoploss_cooldown_min=int(env.get("POST_STOPLOSS_COOLDOWN_MIN", "30")),
        initial_position_fraction=float(env.get("POSITION_FRACTION", "0.40")),
        bar_minutes=5,
        sell_on_next_open=env.get("SELL_ON_NEXT_OPEN", "true").lower() == "true",
    )
    return r.total_return_pct, r.trades, r.win_rate, r.max_drawdown_pct


def main():
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    # (period, fast, slow)
    combos = [
        (None,  None, None),   # 베이스라인 (KAMA OFF)
        (10,  2, 30),   # 기본값
        (5,   2, 30),   # period 축소 (더 민감)
        (20,  2, 30),   # period 확대 (더 느릿)
        (10,  2, 20),   # slow 축소 (횡보 반응 증가)
        (10,  2, 50),   # slow 확대 (횡보 완전 무시)
        (5,   2, 50),   # period 짧고 slow 길게
        (20,  2, 50),   # period 길고 slow 길게
        (15,  2, 40),   # 중간값
    ]

    print(f"\nKAMA tune  period={period}  symbols={', '.join(symbols)}\n")

    # 데이터 다운로드
    dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        print(f"  {sym} ...", end=" ", flush=True)
        try:
            dfs[sym] = _download(sym, period)
            print(f"{len(dfs[sym])}bars")
        except Exception as e:
            print(f"ERR: {e}")

    print()
    print(f"{'설정':<22} {'평균':>8} {'거래':>6}  개별 수익률")
    print("=" * 90)

    baseline = None
    for combo in combos:
        p, f, s = combo
        if p is None:
            # 베이스라인: KAMA OFF
            from backtest_current import _make_current
            fn = _make_current()
            label = "BASE (KAMA OFF)"
        else:
            fn = _make_kama(p, f, s)
            label = f"p={p} fast={f} slow={s}"

        rets, trades_list = [], []
        cells = []
        for sym in symbols:
            if sym not in dfs:
                cells.append("N/A")
                continue
            try:
                ret, trades, wr, mdd = _run(dfs[sym], sym, fn)
                rets.append(ret)
                trades_list.append(trades)
                cells.append(f"{ret:>+6.2f}%({trades}T)")
            except Exception as e:
                cells.append("ERR")

        avg = sum(rets) / len(rets) if rets else 0.0
        avg_t = int(sum(trades_list) / len(trades_list)) if trades_list else 0
        diff = f"({avg - baseline:>+.2f}%p)" if baseline is not None else "(base)"
        if baseline is None:
            baseline = avg
        print(f"{label:<22} {avg:>+8.2f}% {diff:<10} {avg_t:>4}T   " + "  ".join(cells))

    print("=" * 90)


if __name__ == "__main__":
    main()
