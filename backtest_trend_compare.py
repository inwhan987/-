"""Donchian / LinReg / PSAR / KAMA 추세 지표 백테스트 비교.

사용:
  python backtest_trend_compare.py [symbol1,symbol2,...] [period]
  예) python backtest_trend_compare.py 005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS 60d
"""
from __future__ import annotations

import os, sys, tempfile, shutil
from pathlib import Path
from typing import Callable

import pandas as pd

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

from backtest_current import _load_env, _make_current, ATR_STOP_MAX_PCT
from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv


def _make_variant(
    *,
    donchian: bool = False,
    linreg: bool = False,
    psar: bool = False,
    kama: bool = False,
) -> Callable:
    """현재 전략 기반에서 선택한 추세 지표만 추가로 활성화."""
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
        # 4개 추세 지표 (caller가 결정)
        cfg.donchian_enabled  = donchian
        cfg.donchian_period   = _g("ENSEMBLE_DONCHIAN_PERIOD", 20, int)
        cfg.donchian_weight   = _g("ENSEMBLE_DONCHIAN_WEIGHT", 0.225)
        cfg.linreg_enabled    = linreg
        cfg.linreg_period     = _g("ENSEMBLE_LINREG_PERIOD",   30, int)
        cfg.linreg_weight     = _g("ENSEMBLE_LINREG_WEIGHT",   0.225)
        cfg.psar_enabled      = psar
        cfg.psar_step         = _g("ENSEMBLE_PSAR_STEP",       0.02)
        cfg.psar_max_step     = _g("ENSEMBLE_PSAR_MAX_STEP",   0.2)
        cfg.psar_weight       = _g("ENSEMBLE_PSAR_WEIGHT",     0.225)
        cfg.kama_enabled      = kama
        cfg.kama_period       = _g("ENSEMBLE_KAMA_PERIOD",     10, int)
        cfg.kama_fast         = _g("ENSEMBLE_KAMA_FAST",       2, int)
        cfg.kama_slow         = _g("ENSEMBLE_KAMA_SLOW",       30, int)
        cfg.kama_weight       = _g("ENSEMBLE_KAMA_WEIGHT",     0.225)

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
        raise ValueError(f"데이터 없음: {symbol}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    return df


def _run_all(df: pd.DataFrame, symbol: str, fn: Callable) -> tuple[float, int, float, float, float, float]:
    env = _load_env()
    add_buy_enabled = env.get("ADD_BUY_ENABLED", "true").lower() == "true"
    r = run_strategy(
        df, fn, symbol, stop_loss_pct=ATR_STOP_MAX_PCT,
        enable_add_buy=add_buy_enabled,
        add_buy_fraction=float(env.get("ADD_BUY_FRACTION", "0.20")),
        add_buy_max_count=int(env.get("ADD_BUY_MAX_COUNT", "2")),
        add_buy_max_position_pct=float(env.get("ADD_BUY_MAX_POSITION_PCT", "0.80")),
        inherit_initial_stop=env.get("ADD_BUY_INHERIT_INITIAL_STOP", "true").lower() == "true",
        post_stoploss_cooldown_min=int(env.get("POST_STOPLOSS_COOLDOWN_MIN", "30")),
        initial_position_fraction=float(env.get("POSITION_FRACTION", "0.40")),
        bar_minutes=5,
        sell_on_next_open=env.get("SELL_ON_NEXT_OPEN", "true").lower() == "true",
    )
    pf = r.profit_factor if r.profit_factor != float("inf") else 999.0
    return r.total_return_pct, r.trades, r.win_rate, r.max_drawdown_pct, r.sharpe, pf


def main():
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    variants = [
        ("베이스라인",          dict(donchian=False, linreg=False, psar=False, kama=False)),
        ("+Donchian(20)",      dict(donchian=True,  linreg=False, psar=False, kama=False)),
        ("+LinReg(30)",        dict(donchian=False, linreg=True,  psar=False, kama=False)),
        ("+PSAR",              dict(donchian=False, linreg=False, psar=True,  kama=False)),
        ("+KAMA(10)",          dict(donchian=False, linreg=False, psar=False, kama=True )),
        ("+DC+LR+PS+KA 전부",  dict(donchian=True,  linreg=True,  psar=True,  kama=True )),
    ]

    print(f"\n기간: {period}  종목: {', '.join(symbols)}\n")

    # 헤더
    col_w = 20
    sym_w = 14
    hdr = f"{'전략':<{col_w}} " + "  ".join(f"{s:<{sym_w}}" for s in symbols) + f"  {'평균':>8}"
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))

    # 데이터 미리 다운로드
    dfs: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        print(f"  {symbol} 다운로드 중...", end=" ", flush=True)
        try:
            dfs[symbol] = _download(symbol, period)
            print(f"{len(dfs[symbol])}봉", flush=True)
        except Exception as e:
            print(f"오류: {e}")

    print()

    for label, kwargs in variants:
        fn = _make_variant(**kwargs)
        returns = []
        cells = []
        for symbol in symbols:
            if symbol not in dfs:
                cells.append(f"{'N/A':<{sym_w}}")
                continue
            try:
                ret, trades, wr, mdd, sharpe, pf = _run_all(dfs[symbol], symbol, fn)
                returns.append(ret)
                cells.append(f"{ret:>+7.2f}%({trades}T) ")
            except Exception as e:
                cells.append(f"{'ERR':<{sym_w}}")

        avg = sum(returns) / len(returns) if returns else 0.0
        row = f"{label:<{col_w}} " + "  ".join(cells) + f"  {avg:>+8.2f}%"
        print(row)

    print("=" * len(hdr))
    print()
    print("* 가중치 0.225 additive — 기존 VWAP/ST/RSI/BB/DC 합산 점수에 더해짐")


if __name__ == "__main__":
    main()
