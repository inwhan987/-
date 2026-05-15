"""추세 필터 효과 백테스트.

비교 모드:
  A. 현재 (기준)
  B. 일봉 SMA(50) 필터 (종가 기준)
  C. 일봉 Supertrend 필터 (종가 기준)
  D. ADX 필터 (5분봉 ADX < 20 시 매수 차단)
  E. B + D 조합
  F. C + D 조합

사용: python backtest_trend_filter.py [symbols] [period]
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
import numpy as np

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


def _compute_daily_indicators(df_5m: pd.DataFrame) -> pd.DataFrame:
    """5분봉 → 일봉 변환 + SMA50/Supertrend 계산."""
    df_5m = df_5m.copy()
    df_5m.index = pd.to_datetime(df_5m.index)
    # 일봉 OHLCV로 리샘플
    daily = df_5m.resample("D").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()

    # SMA50 (50일이 안 되면 짧은 기간 평균으로 대체)
    daily["sma50"] = daily["close"].rolling(window=50, min_periods=10).mean()

    # Supertrend (p=10, m=3) — 일봉용 보수적 설정
    period, mult = 10, 3.0
    hl2 = (daily["high"] + daily["low"]) / 2
    # ATR
    tr1 = daily["high"] - daily["low"]
    tr2 = (daily["high"] - daily["close"].shift()).abs()
    tr3 = (daily["low"] - daily["close"].shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=1).mean()

    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    direction = pd.Series(index=daily.index, dtype=int)
    direction.iloc[0] = 1
    for i in range(1, len(daily)):
        c = daily["close"].iloc[i]
        prev_dir = direction.iloc[i-1]
        if c > upper.iloc[i-1]:
            direction.iloc[i] = 1
        elif c < lower.iloc[i-1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = prev_dir
            if direction.iloc[i] == 1 and lower.iloc[i] < lower.iloc[i-1]:
                lower.iloc[i] = lower.iloc[i-1]
            if direction.iloc[i] == -1 and upper.iloc[i] > upper.iloc[i-1]:
                upper.iloc[i] = upper.iloc[i-1]
    daily["st_dir"] = direction

    return daily


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """5분봉 ADX 계산."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[(plus_dm - minus_dm) < 0] = 0
    minus_dm[(minus_dm - plus_dm) < 0] = 0

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(window=period, min_periods=1).mean()
    plus_di = 100 * (plus_dm.rolling(window=period, min_periods=1).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period, min_periods=1).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(window=period, min_periods=1).mean()
    return adx


def _make_strategy(use_sma50: bool, use_daily_st: bool, use_adx: bool,
                    daily: pd.DataFrame, adx_series: pd.Series):
    """필터 옵션 조합."""
    env = _load_env()
    def _g(key, default, cast=float):
        try:
            return cast(env[key]) if key in env else default
        except Exception:
            return default

    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        last_price = float(df_slice["close"].iloc[-1])
        now_idx = df_slice.index[-1]
        today_date = now_idx.date()

        # ── 필터: 일봉 데이터는 "어제 종가까지" 기준 ───────────────────
        # 어제 일봉 종가의 SMA50 / ST 방향 확인
        daily_until_yesterday = daily[daily.index.date < today_date]

        if use_sma50 and len(daily_until_yesterday) > 0:
            last_close = daily_until_yesterday["close"].iloc[-1]
            last_sma = daily_until_yesterday["sma50"].iloc[-1]
            if pd.notna(last_sma) and last_close <= last_sma:
                # 일봉 종가 ≤ SMA50 → 매수 차단 (보유분 매도는 정상)
                # 매수 신호만 무시. 정상 매도는 진행.
                pass  # 아래에서 처리

        if use_daily_st and len(daily_until_yesterday) > 0:
            last_st = daily_until_yesterday["st_dir"].iloc[-1]
            if last_st == -1:
                # 일봉 ST 하락추세 → 매수 차단
                pass  # 아래에서 처리

        # ── 기존 전략 ───────────────────────────────────────────
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

        # ── 매수 차단 필터 적용 ─────────────────────────────────
        if sig == "buy":
            block = False
            if use_sma50 and len(daily_until_yesterday) > 0:
                last_close = daily_until_yesterday["close"].iloc[-1]
                last_sma = daily_until_yesterday["sma50"].iloc[-1]
                if pd.notna(last_sma) and last_close <= last_sma:
                    block = True
            if use_daily_st and len(daily_until_yesterday) > 0:
                last_st = daily_until_yesterday["st_dir"].iloc[-1]
                if last_st == -1:
                    block = True
            if use_adx:
                try:
                    current_adx = adx_series.loc[now_idx]
                except (KeyError, ValueError):
                    current_adx = None
                if current_adx is not None and pd.notna(current_adx) and current_adx < 20:
                    block = True

            if block:
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


def _run_mode(label: str, use_sma50: bool, use_daily_st: bool, use_adx: bool,
              symbols: list[str], dfs: dict, dailies: dict, adxs: dict) -> tuple[float, float]:
    print(f"\n{'=' * 80}")
    print(f"▶ {label}  (SMA50={use_sma50}, DailyST={use_daily_st}, ADX={use_adx})")
    print(f"{'=' * 80}")
    hdr = f"{'종목':<14} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7}"
    print(hdr)
    print("-" * len(hdr))

    returns, mdds = [], []
    for symbol in symbols:
        if symbol not in dfs:
            continue
        try:
            fn = _make_strategy(use_sma50, use_daily_st, use_adx,
                                dailies[symbol], adxs[symbol])
            r = run_strategy(dfs[symbol], fn, symbol, stop_loss_pct=5.0)
            print(f"{symbol:<14} "
                  f"{r.total_return_pct:>+8.2f}% "
                  f"{r.trades:>6} "
                  f"{r.win_rate:>6.1f}% "
                  f"{r.max_drawdown_pct:>6.1f}% "
                  f"{r.sharpe:>7.2f}")
            returns.append(r.total_return_pct)
            mdds.append(r.max_drawdown_pct)
        except Exception as e:
            print(f"{symbol:<14} 오류: {e}")
    avg_r = sum(returns) / len(returns) if returns else 0.0
    avg_m = sum(mdds) / len(mdds) if mdds else 0.0
    print("-" * len(hdr))
    print(f"{'평균':>14} {avg_r:>+8.2f}%  MDD {avg_m:.1f}%")
    return avg_r, avg_m


def main():
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    print("데이터 다운로드 중...")
    dfs, dailies, adxs = {}, {}, {}
    for s in symbols:
        print(f"  {s}...", end=" ", flush=True)
        try:
            dfs[s] = _download(s, period)
            dailies[s] = _compute_daily_indicators(dfs[s])
            adxs[s] = _compute_adx(dfs[s])
            print(f"{len(dfs[s])}봉  (일봉 {len(dailies[s])}일)")
        except Exception as e:
            print(f"실패: {e}")

    # 모드별 실행
    modes = [
        ("A. 현재 (기준)",                 False, False, False),
        ("B. 일봉 SMA(50) 필터",          True,  False, False),
        ("C. 일봉 Supertrend 필터",       False, True,  False),
        ("D. ADX(<20) 필터",              False, False, True),
        ("E. SMA(50) + ADX",              True,  False, True),
        ("F. 일봉 ST + ADX",              False, True,  True),
    ]
    avg_results = []
    for label, sma, dst, adx in modes:
        ar, am = _run_mode(label, sma, dst, adx, symbols, dfs, dailies, adxs)
        avg_results.append((label, ar, am))

    base_ret, base_mdd = avg_results[0][1], avg_results[0][2]
    print("\n" + "=" * 80)
    print(" [최종 요약]")
    print("=" * 80)
    print(f"{'설정':<35} {'평균수익':>9} {'평균MDD':>9} {'수익차':>10} {'MDD차':>9}")
    print("-" * 90)
    for label, ar, am in avg_results:
        if label == avg_results[0][0]:
            print(f"{label:<35} {ar:>+8.2f}% {am:>8.1f}% {'-':>10} {'-':>9}")
        else:
            print(f"{label:<35} {ar:>+8.2f}% {am:>8.1f}% {ar-base_ret:>+9.2f}%p {am-base_mdd:>+8.1f}%p")


if __name__ == "__main__":
    main()
