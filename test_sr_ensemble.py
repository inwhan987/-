"""S/R 지지/저항 앙상블 7번째 전략 그리드 테스트 (삼성전자 단독).

사용:
  python test_sr_ensemble.py [period]
  예) python test_sr_ensemble.py 90d
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
import yfinance as yf

from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv
from backtest_current import _load_env, ATR_STOP_MAX_PCT

SYMBOL = "005930.KS"
PERIOD = sys.argv[1] if len(sys.argv) > 1 else "90d"


def _download(symbol: str, period: str) -> pd.DataFrame:
    df = yf.download(symbol, period=period, interval="5m",
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"데이터 없음: {symbol}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    return df


def _make_fn(sr_enabled: bool, sr_weight: float = 0.15,
             sr_lookback: int = 60, sr_swing_window: int = 3,
             sr_proximity_pct: float = 0.010):
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
        cfg.rsi_period                  = _g("TRADE_RSI_PERIOD",             20, int)
        cfg.rsi_oversold                = _g("TRADE_RSI_OVERSOLD",           30.0)
        cfg.rsi_overbought              = _g("TRADE_RSI_OVERBOUGHT",         74.0)
        cfg.supertrend_period           = _g("TRADE_SUPERTREND_PERIOD",      7, int)
        cfg.supertrend_mult             = _g("TRADE_SUPERTREND_MULT",        4.0)
        cfg.bb_window                   = _g("TRADE_BB_WINDOW",  20, int)
        cfg.bb_k                        = _g("TRADE_BB_K",       2.0)
        cfg.bb_consec                   = _g("TRADE_BB_CONSEC",  3, int)
        raw_w = env.get("ENSEMBLE_WEIGHTS", "0.225,0.225,0.225,0.225,0.10")
        try:
            cfg.weights = tuple(float(x) for x in raw_w.split(","))
        except Exception:
            cfg.weights = (0.225, 0.225, 0.225, 0.225, 0.10)
        cfg.min_buy_votes               = _g("ENSEMBLE_MIN_BUY_VOTES",       2, int)
        cfg.buy_threshold               = _g("ENSEMBLE_BUY_THRESHOLD",       0.50)
        cfg.add_buy_threshold           = _g("ADD_BUY_THRESHOLD",            0.60)
        cfg.add_buy_min_votes           = _g("ADD_BUY_MIN_VOTES",            3, int)
        cfg.min_sell_votes              = _g("ENSEMBLE_MIN_SELL_VOTES",      2, int)
        cfg.sell_threshold              = _g("ENSEMBLE_SELL_THRESHOLD",      -0.55)
        cfg.volume_filter_enabled       = False
        cfg.daily_context_profit_gate_pct = _g("DAILY_CONTEXT_PROFIT_GATE_PCT", 1.5)
        cfg.daily_context_avwap_pct       = _g("DAILY_CONTEXT_AVWAP_PCT",       1.5)
        cfg.daily_context_pdh_pct         = _g("DAILY_CONTEXT_PDH_PCT",         1.0)
        cfg.daily_context_pdc_pct         = _g("DAILY_CONTEXT_PDC_PCT",         1.5)
        cfg.daily_context_trend_bonus     = _g("DAILY_CONTEXT_TREND_BONUS",     0.5)
        # S/R
        cfg.sr_enabled       = sr_enabled
        cfg.sr_weight        = sr_weight
        cfg.sr_lookback      = sr_lookback
        cfg.sr_swing_window  = sr_swing_window
        cfg.sr_proximity_pct = sr_proximity_pct

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


COMBOS = [
    # (label, sr_enabled, sr_weight, sr_lookback, sr_swing_window, sr_proximity_pct)
    ("기준(SR off)",           False, 0.00, 60, 3, 0.010),
    ("SR w0.10 sw2 p0.5%",    True,  0.10, 60, 2, 0.005),
    ("SR w0.10 sw2 p1.0%",    True,  0.10, 60, 2, 0.010),
    ("SR w0.10 sw3 p1.0%",    True,  0.10, 60, 3, 0.010),
    ("SR w0.10 sw3 p1.5%",    True,  0.10, 60, 3, 0.015),
    ("SR w0.15 sw2 p0.5%",    True,  0.15, 60, 2, 0.005),
    ("SR w0.15 sw2 p1.0%",    True,  0.15, 60, 2, 0.010),
    ("SR w0.15 sw3 p1.0%",    True,  0.15, 60, 3, 0.010),
    ("SR w0.15 sw3 p1.5%",    True,  0.15, 60, 3, 0.015),
    ("SR w0.20 sw3 p1.0%",    True,  0.20, 60, 3, 0.010),
    ("SR w0.15 lk48 sw3 p1%", True,  0.15, 48, 3, 0.010),
    ("SR w0.15 lk80 sw3 p1%", True,  0.15, 80, 3, 0.010),
]


def main():
    print(f"\n{'='*72}")
    print(f"  S/R 앙상블 전략 그리드 테스트  |  {SYMBOL}  |  {PERIOD}")
    print(f"{'='*72}")
    print(f"  데이터 다운로드 중...", end=" ", flush=True)

    env = _load_env()
    add_buy_enabled = env.get("ADD_BUY_ENABLED", "true").lower() == "true"
    add_buy_frac    = float(env.get("ADD_BUY_FRACTION",         "0.20"))
    add_buy_max     = int(  env.get("ADD_BUY_MAX_COUNT",        "2"))
    add_buy_maxpos  = float(env.get("ADD_BUY_MAX_POSITION_PCT", "0.80"))
    inherit_stop    = env.get("ADD_BUY_INHERIT_INITIAL_STOP", "true").lower() == "true"
    cooldown_min    = int(  env.get("POST_STOPLOSS_COOLDOWN_MIN", "30"))
    pos_frac        = float(env.get("POSITION_FRACTION",        "0.40"))
    sell_on_next    = env.get("SELL_ON_NEXT_OPEN", "true").lower() == "true"

    df = _download(SYMBOL, PERIOD)
    print(f"{len(df)}봉 로드 완료\n")

    fmt = "{:<26} {:>8} {:>7} {:>7} {:>8} {:>7}"
    print(fmt.format("설정", "수익률", "거래수", "승률", "MDD", "샤프"))
    print("-" * 72)

    base_ret = None
    for (label, sr_en, sr_w, sr_lk, sr_sw, sr_prox) in COMBOS:
        fn = _make_fn(sr_en, sr_w, sr_lk, sr_sw, sr_prox)
        r = run_strategy(
            df, fn, label,
            stop_loss_pct=ATR_STOP_MAX_PCT,
            enable_add_buy=add_buy_enabled,
            add_buy_fraction=add_buy_frac,
            add_buy_max_count=add_buy_max,
            add_buy_max_position_pct=add_buy_maxpos,
            inherit_initial_stop=inherit_stop,
            post_stoploss_cooldown_min=cooldown_min,
            initial_position_fraction=pos_frac,
            bar_minutes=5,
            sell_on_next_open=sell_on_next,
        )
        ret = r.total_return_pct
        if base_ret is None:
            base_ret = ret
        diff = f"  ({ret - base_ret:+.2f}%p)" if sr_en else ""
        sharpe = getattr(r, "sharpe_ratio", 0.0) or 0.0
        print(fmt.format(
            label[:26],
            f"{ret:+.2f}%",
            str(r.trades),
            f"{r.win_rate:.1f}%",
            f"{r.max_drawdown_pct:.2f}%",
            f"{sharpe:.2f}",
        ) + diff)

    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
