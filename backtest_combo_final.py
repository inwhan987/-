"""BUY 0.50 + SELL -0.40 + 분할익절 + RSI 가중치 조합 백테스트.

사용:
  python backtest_combo_final.py [symbol] [period]
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


def _make_strategy(
    *,
    buy_threshold: float = 0.50,
    sell_threshold: float = -0.40,
    weights: tuple = (0.25, 0.22, 0.20, 0.18, 0.15),
):
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
        cfg.weights                     = weights
        cfg.min_buy_votes               = _g("ENSEMBLE_MIN_BUY_VOTES",       2, int)
        cfg.buy_threshold               = buy_threshold
        cfg.add_buy_threshold           = _g("ADD_BUY_THRESHOLD",            0.45)
        cfg.add_buy_min_votes           = _g("ADD_BUY_MIN_VOTES",            2, int)
        cfg.min_sell_votes              = _g("ENSEMBLE_MIN_SELL_VOTES",      2, int)
        cfg.sell_threshold              = sell_threshold
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
            stop_pct = min((atr_val * 12.0) / last_price * 100, 5.0)
        else:
            stop_pct = 5.0

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


W_BASE = (0.25, 0.22, 0.20, 0.18, 0.15)   # 현재
W_RSI  = (0.20, 0.20, 0.30, 0.15, 0.15)   # RSI 강화


def _run(label: str, fn, df, tp=None):
    r = run_strategy(
        df, fn, "test", stop_loss_pct=5.0,
        enable_add_buy=False,
        initial_position_fraction=0.70,
        bar_minutes=5,
        take_profit_levels=tp,
    )
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    marker = " <<<"  if r.total_return_pct >= 23.5 else (
             " <<"   if r.total_return_pct >= 22.5 else "")
    print(f"{label:<50} {r.total_return_pct:>+7.2f}% {r.trades:>4} "
          f"{r.win_rate:>5.1f}% {r.max_drawdown_pct:>5.1f}% "
          f"{r.sharpe:>5.2f} {pf:>5}{marker}")
    return r.total_return_pct, r.win_rate, r.max_drawdown_pct, r.sharpe


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"

    print(f"종목: {symbol}  기간: {period}")
    print("다운로드 중...", end=" ", flush=True)
    df = _download(symbol, period)
    print(f"{len(df)}봉\n")

    hdr = (f"{'설정':<50} {'수익률':>7} {'거래':>4} {'승률':>6} "
           f"{'MDD':>6} {'샤프':>5} {'손익비':>5}")
    print(hdr)
    print("=" * len(hdr))

    # ── 기준선 ───────────────────────────────────────────────────────────
    print("\n[기준선]")
    _run("현재 설정 (BUY 0.40 / SELL -0.30 / 현재 가중치)",
         _make_strategy(buy_threshold=0.40, sell_threshold=-0.30, weights=W_BASE), df)

    # ── 단일 변수 ────────────────────────────────────────────────────────
    print("\n[단일 변수 효과]")
    _run("BUY 0.50 / SELL -0.40 / 현재 가중치",
         _make_strategy(buy_threshold=0.50, sell_threshold=-0.40, weights=W_BASE), df)
    _run("BUY 0.40 / SELL -0.30 / RSI 강화 가중치",
         _make_strategy(buy_threshold=0.40, sell_threshold=-0.30, weights=W_RSI), df)

    base_fn    = _make_strategy(buy_threshold=0.50, sell_threshold=-0.40, weights=W_BASE)
    base_rsi   = _make_strategy(buy_threshold=0.50, sell_threshold=-0.40, weights=W_RSI)

    # ── BUY 0.50 + SELL -0.40 + 분할 익절 조합 ──────────────────────────
    print("\n[BUY 0.50 + SELL -0.40 + 분할 익절 (현재 가중치)]")
    _run("분할익절 없음 (기준)",
         base_fn, df, tp=None)
    _run("+3%->30%, +5%->30%",
         base_fn, df, tp=[(3.0, 0.30), (5.0, 0.30)])
    _run("+3%->50%",
         base_fn, df, tp=[(3.0, 0.50)])
    _run("+2%->30%, +4%->30%",
         base_fn, df, tp=[(2.0, 0.30), (4.0, 0.30)])
    _run("+5%->50%",
         base_fn, df, tp=[(5.0, 0.50)])
    _run("+3%->30%, +5%->50%",
         base_fn, df, tp=[(3.0, 0.30), (5.0, 0.50)])

    # ── BUY 0.50 + SELL -0.40 + RSI 가중치 ──────────────────────────────
    print("\n[BUY 0.50 + SELL -0.40 + RSI 강화 가중치]")
    _run("분할익절 없음",
         base_rsi, df, tp=None)
    _run("+3%->30%, +5%->30%",
         base_rsi, df, tp=[(3.0, 0.30), (5.0, 0.30)])
    _run("+3%->50%",
         base_rsi, df, tp=[(3.0, 0.50)])
    _run("+2%->30%, +4%->30%",
         base_rsi, df, tp=[(2.0, 0.30), (4.0, 0.30)])
    _run("+5%->50%",
         base_rsi, df, tp=[(5.0, 0.50)])
    _run("+3%->30%, +5%->50%",
         base_rsi, df, tp=[(3.0, 0.30), (5.0, 0.50)])

    print("\n<<< = 수익률 23.5%+ (현재 최고 기록 경신)")
    print("<<  = 수익률 22.5%+")


if __name__ == "__main__":
    main()
