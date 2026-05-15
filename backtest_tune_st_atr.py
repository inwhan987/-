"""Supertrend 기간 / ATR 배수 비교 백테스트.

6번 (Supertrend 기간 7 vs 10) 과 7번 (ATR 배수 12 vs 8) 을 따로 비교.

사용:
  python backtest_tune_st_atr.py [symbols] [period]
  예) python backtest_tune_st_atr.py 005930.KS,035720.KS 60d
"""
from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd

from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv


def _load_env() -> dict[str, str]:
    """.env / .env.overrides 통합 읽기."""
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


def _make_strategy(st_period: int, atr_multiplier: float, atr_max_pct: float):
    """현재 설정 기반에서 st_period / atr_multiplier 만 오버라이드."""
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
        # ★ ST 기간만 override
        cfg.supertrend_period           = st_period
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

        # ATR 동적 손절 (배수/캡 override)
        last_price = float(df_slice["close"].iloc[-1])
        ohlcv_list = [
            {"open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume}
            for r in df_slice.itertuples()
        ]
        atr_val = atr_from_ohlcv(ohlcv_list, period=14)
        if atr_val > 0 and last_price > 0:
            dynamic_pct = (atr_val * atr_multiplier) / last_price * 100
            stop_pct = min(dynamic_pct, atr_max_pct)
        else:
            stop_pct = atr_max_pct

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


def _run_one_config(label: str, st_period: int, atr_multiplier: float, atr_max_pct: float,
                    symbols: list[str], period: str, dfs: dict) -> None:
    print(f"\n{'=' * 80}")
    print(f"▶ {label}  (ST_PERIOD={st_period}, ATR_MULT={atr_multiplier}, ATR_MAX={atr_max_pct})")
    print(f"{'=' * 80}")
    hdr = f"{'종목':<14} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7}"
    print(hdr)
    print("-" * len(hdr))

    fn = _make_strategy(st_period, atr_multiplier, atr_max_pct)
    returns = []
    for symbol in symbols:
        try:
            df = dfs[symbol]
            r = run_strategy(df, fn, symbol, stop_loss_pct=atr_max_pct)
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
            returns.append(r.total_return_pct)
        except Exception as e:
            print(f"{symbol:<14} 오류: {e}")
    if returns:
        avg = sum(returns) / len(returns)
        print("-" * len(hdr))
        print(f"{'평균':>14} {avg:>+8.2f}%")
    return sum(returns) / len(returns) if returns else 0.0


def main():
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    # 데이터 1회만 다운로드
    print("데이터 다운로드 중...")
    dfs = {}
    for s in symbols:
        try:
            print(f"  {s}...", end=" ", flush=True)
            dfs[s] = _download(s, period)
            print(f"{len(dfs[s])}봉")
        except Exception as e:
            print(f"실패: {e}")

    env = _load_env()
    cur_st  = int(env.get("TRADE_SUPERTREND_PERIOD", "7"))
    cur_atr = float(env.get("ATR_STOP_MULTIPLIER", "12.0"))
    cur_max = float(env.get("ATR_STOP_MAX_PCT", "5.0"))

    # ── 6번 비교: Supertrend 기간 (ATR 그대로) ─────────────────────────────────
    print("\n" + "#" * 80)
    print(" 【6번】 Supertrend 기간 비교 (ATR 배수 / 캡은 현재값 고정)")
    print("#" * 80)
    avg_st7  = _run_one_config("ST_PERIOD=7  (현재)",  7,  cur_atr, cur_max, symbols, period, dfs)
    avg_st10 = _run_one_config("ST_PERIOD=10 (제안)", 10, cur_atr, cur_max, symbols, period, dfs)

    print(f"\n  → ST=7 평균 {avg_st7:+.2f}% vs ST=10 평균 {avg_st10:+.2f}%  "
          f"(차이 {avg_st10 - avg_st7:+.2f}%p)")

    # ── 7번 비교: ATR 배수 (ST 그대로) ──────────────────────────────────────────
    print("\n" + "#" * 80)
    print(" 【7번】 ATR 배수 비교 (Supertrend 기간은 현재값 고정)")
    print("#" * 80)
    avg_a12 = _run_one_config("ATR_MULT=12 (현재)",  cur_st, 12.0, cur_max, symbols, period, dfs)
    avg_a10 = _run_one_config("ATR_MULT=10 (중간)",  cur_st, 10.0, cur_max, symbols, period, dfs)
    avg_a8  = _run_one_config("ATR_MULT=8  (제안)",  cur_st, 8.0,  cur_max, symbols, period, dfs)

    print(f"\n  → ATR×12 평균 {avg_a12:+.2f}% / ATR×10 평균 {avg_a10:+.2f}% / ATR×8 평균 {avg_a8:+.2f}%")

    # ── 보너스: ATR_MAX_PCT 캡 비교 ────────────────────────────────────────────
    print("\n" + "#" * 80)
    print(" 【7-2】 ATR_MAX_PCT 캡 비교 (배수는 현재값 12, 캡만 변경)")
    print("#" * 80)
    avg_m50 = _run_one_config("ATR_MAX=5.0 (현재)", cur_st, cur_atr, 5.0, symbols, period, dfs)
    avg_m35 = _run_one_config("ATR_MAX=3.5 (보수)", cur_st, cur_atr, 3.5, symbols, period, dfs)
    avg_m25 = _run_one_config("ATR_MAX=2.5 (타이트)", cur_st, cur_atr, 2.5, symbols, period, dfs)

    print(f"\n  → MAX=5.0 평균 {avg_m50:+.2f}% / MAX=3.5 평균 {avg_m35:+.2f}% / MAX=2.5 평균 {avg_m25:+.2f}%")

    # ── 최종 요약 ─────────────────────────────────────────────────────────────
    print("\n" + "═" * 80)
    print(" 📊 최종 요약")
    print("═" * 80)
    print(f"  [6번] ST_PERIOD:  7={avg_st7:+.2f}%  vs  10={avg_st10:+.2f}%")
    print(f"  [7번] ATR_MULT:  12={avg_a12:+.2f}%  10={avg_a10:+.2f}%  8={avg_a8:+.2f}%")
    print(f"  [7-2] ATR_MAX:   5.0={avg_m50:+.2f}%  3.5={avg_m35:+.2f}%  2.5={avg_m25:+.2f}%")


if __name__ == "__main__":
    main()
