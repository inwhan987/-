"""안전장치 효과 비교 백테스트 (자본 활용도 동일).

비교 (모두 40% 진입):
  A. 안전장치 없음 (기본 백테스트, 추가매수 X)
  B. 추가매수만
  C. 추가매수 + 손절선 잠금
  D. 추가매수 + 손절선 잠금 + 쿨다운 (현재 실전)
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
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

from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv

ATR_STOP_MAX_PCT = 5.0


def _load_env() -> dict[str, str]:
    root = Path(__file__).parent
    result: dict[str, str] = {}
    for fname in (".env", ".env.overrides"):
        p = root / fname
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.split("#")[0].strip()
    return result


def _make_strategy():
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
        cfg.bb_window                   = 20
        cfg.bb_k                        = 2.0
        cfg.bb_consec                   = 3
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
        cfg.volume_high_ratio           = _g("ENSEMBLE_VOLUME_HIGH_RATIO",   1.2)
        cfg.volume_low_ratio            = _g("ENSEMBLE_VOLUME_LOW_RATIO",    0.7)
        cfg.volume_score_boost          = _g("ENSEMBLE_VOLUME_SCORE_BOOST",  0.10)
        cfg.volume_score_penalty        = _g("ENSEMBLE_VOLUME_SCORE_PENALTY",0.05)
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


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"

    print(f"종목: {symbol}  기간: {period}")
    print("다운로드 중...", end=" ", flush=True)
    df = _download(symbol, period)
    print(f"{len(df)}봉\n")

    # 모든 모드 모두 진입 비율 40% (실전과 동일)
    # (label, enable_add_buy, inherit_stop, cooldown_min)
    modes = [
        # 95% 진입 (기본 백테스트와 비교 가능)
        ("기본 백테스트 (95% 진입)",         0.95, False, False, 0),
        # 40% 진입 시리즈
        ("A. 40% 진입, 안전장치 없음",       0.40, False, False, 0),
        ("B. 40% + 추가매수만",              0.40, True,  False, 0),
        ("C. 40% + 추가매수 + 손절선 잠금",  0.40, True,  True,  0),
        ("D. 현재 실전 (모든 안전장치)",     0.40, True,  True,  30),
    ]

    fn = _make_strategy()
    print(f"{'설정':<38} {'수익률':>8} {'거래':>5} {'승률':>6} {'MDD':>6} {'샤프':>6} {'손익비':>6}")
    print("-" * 85)
    for label, frac, add_buy, inherit, cooldown in modes:
        try:
            r = run_strategy(
                df, fn, symbol, stop_loss_pct=ATR_STOP_MAX_PCT,
                enable_add_buy=add_buy,
                add_buy_fraction=0.20,
                add_buy_max_count=2,
                add_buy_max_position_pct=0.80,
                inherit_initial_stop=inherit,
                post_stoploss_cooldown_min=cooldown,
                initial_position_fraction=frac,
                bar_minutes=5,
            )
            pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
            print(f"{label:<38} "
                  f"{r.total_return_pct:>+7.2f}% "
                  f"{r.trades:>5} "
                  f"{r.win_rate:>5.1f}% "
                  f"{r.max_drawdown_pct:>5.1f}% "
                  f"{r.sharpe:>6.2f} "
                  f"{pf:>6}")
        except Exception as e:
            print(f"{label:<38} 오류: {e}")


if __name__ == "__main__":
    main()
