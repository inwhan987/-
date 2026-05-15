"""속도 기반 긴급매도 + 일중 고점 트레일링 효과 백테스트.

4가지 모드 비교:
  A. 현재 (기준선)
  B. + 속도 매도 (10분간 -2%)
  C. + 트레일링 (고점 대비 -2%)
  D. A+B+C 다 적용

사용: python backtest_velocity_trailing.py [symbols] [period]
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
from pathlib import Path

# Windows 한글 경로 SSL 우회
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


# 속도/트레일링 파라미터
VELOCITY_BARS = 2          # 직전 2봉 (10분)
VELOCITY_DROP_PCT = -2.0   # -2% 이상 빠지면 매도
TRAILING_FROM_HIGH = -2.0  # 일중 고점 대비 -2% 빠지면 매도


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


def _make_strategy(velocity_enabled: bool, trailing_enabled: bool):
    """속도/트레일링 옵션을 켜고 끄는 전략 빌더."""
    env = _load_env()
    def _g(key, default, cast=float):
        try:
            return cast(env[key]) if key in env else default
        except Exception:
            return default

    # 상태 추적 (포지션 진입 시점부터 고점 / 진입 인덱스)
    state = {"max_since_entry": 0.0, "last_qty": 0, "entry_date": None}

    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        last_price = float(df_slice["close"].iloc[-1])
        today_date = df_slice.index[-1].date()

        # ── 상태 갱신: 신규 진입 / 청산 감지 ────────────────────────
        if position_qty > 0 and state["last_qty"] == 0:
            # 신규 진입
            state["max_since_entry"] = last_price
            state["entry_date"] = today_date
        elif position_qty == 0:
            # 청산됨
            state["max_since_entry"] = 0.0
            state["entry_date"] = None
        state["last_qty"] = position_qty

        # 고점 갱신
        if position_qty > 0:
            state["max_since_entry"] = max(state["max_since_entry"], last_price)

        # ── (B) 속도 기반 긴급매도 ─────────────────────────────────
        if velocity_enabled and position_qty > 0:
            n_bars = VELOCITY_BARS + 1  # +1 (start point)
            if len(df_slice) >= n_bars:
                start_price = float(df_slice["close"].iloc[-n_bars])
                drop_pct = (last_price / start_price - 1) * 100
                if drop_pct <= VELOCITY_DROP_PCT:
                    return "sell"

        # ── (C) 일중 고점 트레일링 ─────────────────────────────────
        if trailing_enabled and position_qty > 0 and state["max_since_entry"] > 0:
            drop_from_high = (last_price / state["max_since_entry"] - 1) * 100
            if drop_from_high <= TRAILING_FROM_HIGH:
                return "sell"

        # ── (A) 기존 전략 ──────────────────────────────────────────
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

        # ATR 손절
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


def _run_mode(label: str, velocity: bool, trailing: bool,
              symbols: list[str], dfs: dict) -> tuple[float, float]:
    """모드별 백테스트 실행. (평균 수익률, 평균 MDD) 반환."""
    print(f"\n{'=' * 80}")
    print(f"▶ {label}  (velocity={velocity}, trailing={trailing})")
    print(f"{'=' * 80}")
    hdr = f"{'종목':<14} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7}"
    print(hdr)
    print("-" * len(hdr))

    fn = _make_strategy(velocity, trailing)
    returns, mdds = [], []
    for symbol in symbols:
        try:
            df = dfs[symbol]
            r = run_strategy(df, fn, symbol, stop_loss_pct=5.0)
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
    dfs = {}
    for s in symbols:
        try:
            print(f"  {s}...", end=" ", flush=True)
            dfs[s] = _download(s, period)
            print(f"{len(dfs[s])}봉")
        except Exception as e:
            print(f"실패: {e}")

    print(f"\n속도 매도: 직전 {VELOCITY_BARS}봉({VELOCITY_BARS*5}분)간 {VELOCITY_DROP_PCT}% 이상 하락")
    print(f"트레일링: 일중 고점 대비 {TRAILING_FROM_HIGH}% 이상 하락")

    r_a, m_a = _run_mode("A. 현재 (기준선)",       False, False, symbols, dfs)
    r_b, m_b = _run_mode("B. + 속도 매도",         True,  False, symbols, dfs)
    r_c, m_c = _run_mode("C. + 트레일링",          False, True,  symbols, dfs)
    r_d, m_d = _run_mode("D. + 속도+트레일링",     True,  True,  symbols, dfs)

    print("\n" + "=" * 80)
    print(" [최종 요약]")
    print("=" * 80)
    print(f"  A. 현재         : {r_a:+.2f}%  MDD {m_a:.1f}%")
    print(f"  B. +속도        : {r_b:+.2f}%  MDD {m_b:.1f}%  (수익차 {r_b - r_a:+.2f}%p, MDD차 {m_b - m_a:+.1f}%p)")
    print(f"  C. +트레일링    : {r_c:+.2f}%  MDD {m_c:.1f}%  (수익차 {r_c - r_a:+.2f}%p, MDD차 {m_c - m_a:+.1f}%p)")
    print(f"  D. +속도+트레일 : {r_d:+.2f}%  MDD {m_d:.1f}%  (수익차 {r_d - r_a:+.2f}%p, MDD차 {m_d - m_a:+.1f}%p)")


if __name__ == "__main__":
    main()
