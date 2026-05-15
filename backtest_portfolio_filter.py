"""포트폴리오 단위 추세 필터 + 속도 매도 조합 백테스트.

아이디어:
  1. 다종목 운용에서 일봉 추세 좋은 종목만 거래 (포트폴리오 필터)
  2. 그 종목들에서 갑자기 급락 시 속도 매도로 보호

비교:
  A. 현재 (모든 종목, 필터 없음)
  B. SMA(20) 필터만
  C. SMA(20) 필터 + 속도 매도 -4% (20분)
  D. SMA(10) 필터 + 속도 매도 -4% (20분)
  E. 5일 추세 (직전 5일 평균 < 현재) + 속도 매도
  F. SMA(20) + 속도 매도 + 트레일링 -3% 수익권만

사용: python backtest_portfolio_filter.py [symbols] [period]
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


def _compute_daily(df_5m: pd.DataFrame) -> pd.DataFrame:
    daily = df_5m.resample("D").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    daily["sma10"] = daily["close"].rolling(window=10, min_periods=3).mean()
    daily["sma20"] = daily["close"].rolling(window=20, min_periods=5).mean()
    daily["sma5_close"] = daily["close"].rolling(window=5, min_periods=2).mean()
    return daily


def _make_strategy(filter_mode: str, velocity_pct: float, trailing_pct: float,
                   trailing_profit_only: bool, daily: pd.DataFrame):
    """
    filter_mode:
      'none' - 필터 없음
      'sma20' - 어제 종가 > SMA(20)
      'sma10' - 어제 종가 > SMA(10)
      'trend5' - 어제 종가 > 직전 5일 평균
    velocity_pct: 음수 (예: -4.0). 0이면 비활성. 20분 (4봉) 기준.
    trailing_pct: 음수 (예: -3.0). 0이면 비활성.
    """
    env = _load_env()
    def _g(key, default, cast=float):
        try:
            return cast(env[key]) if key in env else default
        except Exception:
            return default

    state = {"max_since_entry": 0.0, "last_qty": 0}

    def _is_trending_up(now_date) -> bool:
        """주어진 날짜 기준 어제까지 추세 좋은지 판단."""
        if filter_mode == "none":
            return True
        until = daily[daily.index.date < now_date]
        if len(until) == 0:
            return True
        last = until.iloc[-1]
        if filter_mode == "sma20":
            return pd.notna(last["sma20"]) and last["close"] > last["sma20"]
        if filter_mode == "sma10":
            return pd.notna(last["sma10"]) and last["close"] > last["sma10"]
        if filter_mode == "trend5":
            return pd.notna(last["sma5_close"]) and last["close"] > last["sma5_close"]
        return True

    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        last_price = float(df_slice["close"].iloc[-1])
        today_date = df_slice.index[-1].date()

        # 상태 업데이트 (속도/트레일링용)
        if position_qty > 0 and state["last_qty"] == 0:
            state["max_since_entry"] = last_price
        elif position_qty == 0:
            state["max_since_entry"] = 0.0
        state["last_qty"] = position_qty
        if position_qty > 0:
            state["max_since_entry"] = max(state["max_since_entry"], last_price)

        # ── 속도 기반 긴급매도 (-4% 20분) ─────────────────────────
        if velocity_pct < 0 and position_qty > 0:
            n_bars = 5  # 20분 = 4봉, +1 = 5
            if len(df_slice) >= n_bars:
                start_price = float(df_slice["close"].iloc[-n_bars])
                drop = (last_price / start_price - 1) * 100
                if drop <= velocity_pct:
                    return "sell"

        # ── 트레일링 ────────────────────────────────────────────
        if trailing_pct < 0 and position_qty > 0 and state["max_since_entry"] > 0:
            if trailing_profit_only and avg_price > 0 and last_price <= avg_price:
                pass
            else:
                drop_from_high = (last_price / state["max_since_entry"] - 1) * 100
                if drop_from_high <= trailing_pct:
                    return "sell"

        # ── 기존 전략 ──────────────────────────────────────────
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

        ohlcv_list = [
            {"open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume}
            for r in df_slice.itertuples()
        ]
        atr_val = atr_from_ohlcv(ohlcv_list, period=14)
        if atr_val > 0 and last_price > 0:
            dynamic_pct = (atr_val * _g("ATR_STOP_MULTIPLIER", 12.0)) / last_price * 100
            stop_pct = min(dynamic_pct, _g("ATR_STOP_MAX_PCT", 5.0))
        else:
            stop_pct = _g("ATR_STOP_MAX_PCT", 5.0)

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
        sig = decision.signal.value

        # ── 매수만 필터 적용 (매도는 정상 진행) ─────────────────
        if sig == "buy" and not _is_trending_up(today_date):
            return "hold"

        return sig
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


def _run_mode(label: str, filter_mode: str, velocity: float, trailing: float, prof_only: bool,
              symbols: list[str], dfs: dict, dailies: dict) -> tuple[float, float, dict]:
    print(f"\n{'=' * 80}")
    print(f"▶ {label}")
    print(f"{'=' * 80}")
    hdr = f"{'종목':<14} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7}"
    print(hdr)
    print("-" * len(hdr))

    returns, mdds, per_sym = [], [], {}
    for s in symbols:
        if s not in dfs:
            continue
        try:
            fn = _make_strategy(filter_mode, velocity, trailing, prof_only, dailies[s])
            r = run_strategy(dfs[s], fn, s, stop_loss_pct=5.0)
            print(f"{s:<14} "
                  f"{r.total_return_pct:>+8.2f}% "
                  f"{r.trades:>6} "
                  f"{r.win_rate:>6.1f}% "
                  f"{r.max_drawdown_pct:>6.1f}% "
                  f"{r.sharpe:>7.2f}")
            returns.append(r.total_return_pct)
            mdds.append(r.max_drawdown_pct)
            per_sym[s] = r.total_return_pct
        except Exception as e:
            print(f"{s:<14} 오류: {e}")
    avg_r = sum(returns) / len(returns) if returns else 0.0
    avg_m = sum(mdds) / len(mdds) if mdds else 0.0
    print("-" * len(hdr))
    print(f"{'평균':>14} {avg_r:>+8.2f}%  MDD {avg_m:.1f}%")
    return avg_r, avg_m, per_sym


def main():
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    print("데이터 다운로드 중...")
    dfs, dailies = {}, {}
    for s in symbols:
        print(f"  {s}...", end=" ", flush=True)
        try:
            dfs[s] = _download(s, period)
            dailies[s] = _compute_daily(dfs[s])
            print(f"{len(dfs[s])}봉  (일봉 {len(dailies[s])}일)")
        except Exception as e:
            print(f"실패: {e}")

    # 모드 정의: (label, filter_mode, velocity, trailing, profit_only)
    modes = [
        ("A. 현재 (필터 없음)",                       "none",   0,    0,    False),
        ("B. SMA(20) 필터",                          "sma20",  0,    0,    False),
        ("C. SMA(20) + 속도 -4%(20분)",              "sma20", -4.0,  0,    False),
        ("D. SMA(10) + 속도 -4%(20분)",              "sma10", -4.0,  0,    False),
        ("E. 5일추세 + 속도 -4%(20분)",              "trend5",-4.0,  0,    False),
        ("F. SMA(20) + 속도 -4% + 트레일링 -3% 수익권", "sma20", -4.0, -3.0, True),
        ("G. SMA(10) + 트레일링 -3% 수익권",          "sma10",  0,   -3.0, True),
    ]

    results = []
    for label, fm, vel, trail, prof in modes:
        ar, am, per = _run_mode(label, fm, vel, trail, prof, symbols, dfs, dailies)
        results.append((label, ar, am, per))

    base_ret = results[0][1]
    base_mdd = results[0][2]

    print("\n" + "=" * 90)
    print(" [종목별 비교]")
    print("=" * 90)
    header = f"{'설정':<40}" + "".join(f"{s.split('.')[0]:>9}" for s in symbols) + f"{'평균':>9}"
    print(header)
    print("-" * len(header))
    for label, ar, am, per in results:
        line = f"{label:<40}"
        for s in symbols:
            v = per.get(s, 0)
            line += f"{v:>+8.2f}%"
        line += f"{ar:>+8.2f}%"
        print(line)

    print("\n" + "=" * 80)
    print(" [최종 요약]")
    print("=" * 80)
    print(f"{'설정':<42} {'평균수익':>9} {'MDD':>7} {'수익차':>10} {'MDD차':>9}")
    print("-" * 90)
    for label, ar, am, _ in results:
        if label == results[0][0]:
            print(f"{label:<42} {ar:>+8.2f}% {am:>6.1f}% {'-':>10} {'-':>9}")
        else:
            print(f"{label:<42} {ar:>+8.2f}% {am:>6.1f}% {ar-base_ret:>+9.2f}%p {am-base_mdd:>+8.1f}%p")


if __name__ == "__main__":
    main()
