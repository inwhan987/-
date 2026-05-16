"""트레일링 스톱 심화 백테스트.

목적: 최적 trailing_pct 및 발동 임계값(activate_pct) 탐색
기준: BUY 0.50 + SELL -0.40 (이전 조합 백테스트 최적값)

테스트 변수:
  A. trailing_pct   : -2.0 ~ -6.0 (0.5 단위)
  B. activate_pct   : 0.0, 1.0, 2.0, 3.0 (발동 최소 수익 %)
      → 수익 X% 이상 벌었을 때만 트레일링 시작 (조기 손절 방지)
  C. ATR 기반 트레일링: ATR × N 을 trailing 거리로 사용 (변동성 적응)

사용:
  python backtest_trailing_deep.py [symbol] [period]
  예) python backtest_trailing_deep.py 005930.KS 60d
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


def _make_strategy(
    *,
    buy_threshold: float = 0.50,
    sell_threshold: float = -0.40,
    # ── 고정 % 트레일링 ─────────────────────────────────────────
    trailing_pct: float = 0.0,          # 0이면 비활성. 음수로 지정 (예: -4.0)
    activate_profit_pct: float = 0.0,   # 이 수익% 초과 시에만 트레일링 발동
    # ── ATR 기반 트레일링 ──────────────────────────────────────
    atr_trailing_mult: float = 0.0,     # 0이면 비활성. ATR × N 을 trailing 거리로 사용
    atr_trailing_period: int = 14,
    atr_activate_profit_pct: float = 0.0,  # ATR 트레일링 최소 발동 수익%
):
    env = _load_env()
    def _g(key, default, cast=float):
        try:
            return cast(env[key]) if key in env else default
        except Exception:
            return default

    state = {
        "max_since_entry": 0.0,
        "last_qty": 0,
        "atr_high_water": 0.0,   # ATR 트레일링용 고점
    }

    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        last_price = float(df_slice["close"].iloc[-1])

        # ── 상태 업데이트 ─────────────────────────────────────────
        if position_qty > 0 and state["last_qty"] == 0:
            # 신규 진입
            state["max_since_entry"] = last_price
            state["atr_high_water"] = last_price
        elif position_qty == 0:
            state["max_since_entry"] = 0.0
            state["atr_high_water"] = 0.0
        state["last_qty"] = position_qty

        if position_qty > 0:
            state["max_since_entry"] = max(state["max_since_entry"], last_price)
            state["atr_high_water"] = max(state["atr_high_water"], last_price)

        # ── 고정 % 트레일링 ───────────────────────────────────────
        if trailing_pct < 0 and position_qty > 0 and avg_price > 0:
            unrealized_pct = (last_price / avg_price - 1) * 100
            # 발동 조건: 수익이 activate_profit_pct 초과한 적이 있어야 함
            peak_profit_pct = (state["max_since_entry"] / avg_price - 1) * 100
            if peak_profit_pct >= activate_profit_pct:
                drop_from_peak = (last_price / state["max_since_entry"] - 1) * 100
                if drop_from_peak <= trailing_pct:
                    return "sell"

        # ── ATR 기반 트레일링 ────────────────────────────────────
        if atr_trailing_mult > 0 and position_qty > 0 and avg_price > 0:
            ohlcv_list = [
                {"open": r.open, "high": r.high, "low": r.low,
                 "close": r.close, "volume": r.volume}
                for r in df_slice.itertuples()
            ]
            atr_val = atr_from_ohlcv(ohlcv_list, period=atr_trailing_period)
            if atr_val > 0:
                trailing_dist_pct = (atr_val * atr_trailing_mult / last_price) * 100
                peak_profit_pct = (state["atr_high_water"] / avg_price - 1) * 100
                if peak_profit_pct >= atr_activate_profit_pct:
                    drop_pct = (last_price / state["atr_high_water"] - 1) * 100
                    if drop_pct <= -trailing_dist_pct:
                        return "sell"

        # ── 앙상블 시그널 ─────────────────────────────────────────
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
        cfg.weights                     = (0.25, 0.22, 0.20, 0.18, 0.15)
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

        ohlcv_list = [
            {"open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume}
            for r in df_slice.itertuples()
        ]
        atr_val = atr_from_ohlcv(ohlcv_list, period=14)
        if atr_val > 0 and last_price > 0:
            dynamic_pct = (atr_val * 12.0) / last_price * 100
            stop_pct = min(dynamic_pct, 5.0)
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


def _run(label: str, fn, df: pd.DataFrame, frac: float = 0.70) -> tuple:
    r = run_strategy(
        df, fn, "test", stop_loss_pct=5.0,
        enable_add_buy=False,
        initial_position_fraction=frac,
        bar_minutes=5,
    )
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    hold_h = r.avg_hold_bars * 5 / 60  # 봉→시간
    marker = " ★" if r.total_return_pct > 23.0 and r.max_drawdown_pct < 13.0 else ""
    print(f"{label:<52} {r.total_return_pct:>+7.2f}% {r.trades:>4} {r.win_rate:>5.1f}% "
          f"{r.max_drawdown_pct:>5.1f}% {r.sharpe:>5.2f} {pf:>5} {hold_h:>5.1f}h{marker}")
    return r.total_return_pct, r.win_rate, r.max_drawdown_pct, r.sharpe


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"

    print(f"종목: {symbol}  기간: {period}")
    print("다운로드 중...", end=" ", flush=True)
    df = _download(symbol, period)
    print(f"{len(df)}봉\n")

    hdr = (f"{'설정':<52} {'수익률':>7} {'거래':>4} {'승률':>6} "
           f"{'MDD':>6} {'샤프':>5} {'손익비':>5} {'평균보유':>7}")
    print(hdr)
    print("=" * len(hdr))

    # ── 기준선: BUY 0.50 + SELL -0.40, 트레일링 없음 ─────────────────
    print("\n[기준선: BUY 0.50 / SELL -0.40, 트레일링 없음]")
    _run("기준 (트레일링 없음)",
         _make_strategy(buy_threshold=0.50, sell_threshold=-0.40), df)

    # ── A. 고정 % 트레일링 (수익권 진입 즉시 발동) ─────────────────────
    print("\n[A. 수익권 진입 즉시 발동 (activate=0%)]")
    for pct in [-2.0, -2.5, -3.0, -3.5, -4.0, -4.5, -5.0, -6.0]:
        _run(f"  trailing {pct:+.1f}%  activate≥0%",
             _make_strategy(buy_threshold=0.50, sell_threshold=-0.40,
                            trailing_pct=pct, activate_profit_pct=0.0), df)

    # ── B. 발동 임계값: +1% 수익 이후 ─────────────────────────────────
    print("\n[B. +1% 수익 후 발동 (조기 손절 방지)]")
    for pct in [-2.0, -2.5, -3.0, -3.5, -4.0, -4.5, -5.0, -6.0]:
        _run(f"  trailing {pct:+.1f}%  activate≥+1%",
             _make_strategy(buy_threshold=0.50, sell_threshold=-0.40,
                            trailing_pct=pct, activate_profit_pct=1.0), df)

    # ── C. 발동 임계값: +2% 수익 이후 ─────────────────────────────────
    print("\n[C. +2% 수익 후 발동 (노이즈 구간 통과 후)]")
    for pct in [-2.0, -2.5, -3.0, -3.5, -4.0, -4.5, -5.0, -6.0]:
        _run(f"  trailing {pct:+.1f}%  activate≥+2%",
             _make_strategy(buy_threshold=0.50, sell_threshold=-0.40,
                            trailing_pct=pct, activate_profit_pct=2.0), df)

    # ── D. 발동 임계값: +3% 수익 이후 ─────────────────────────────────
    print("\n[D. +3% 수익 후 발동 (강한 추세 확인 후)]")
    for pct in [-3.0, -3.5, -4.0, -4.5, -5.0, -6.0]:
        _run(f"  trailing {pct:+.1f}%  activate≥+3%",
             _make_strategy(buy_threshold=0.50, sell_threshold=-0.40,
                            trailing_pct=pct, activate_profit_pct=3.0), df)

    # ── E. ATR 기반 트레일링 ────────────────────────────────────────────
    print("\n[E. ATR 기반 트레일링 (변동성 적응형)]")
    for mult in [1.0, 1.5, 2.0, 2.5, 3.0]:
        _run(f"  ATR×{mult:.1f}  activate≥0%",
             _make_strategy(buy_threshold=0.50, sell_threshold=-0.40,
                            atr_trailing_mult=mult, atr_activate_profit_pct=0.0), df)
    print()
    for mult in [1.0, 1.5, 2.0, 2.5, 3.0]:
        _run(f"  ATR×{mult:.1f}  activate≥+2%",
             _make_strategy(buy_threshold=0.50, sell_threshold=-0.40,
                            atr_trailing_mult=mult, atr_activate_profit_pct=2.0), df)

    # ── F. 최종 조합 후보 (위 결과에서 best 예상) ─────────────────────
    print("\n[F. 조합 후보 - 수익권만 + 발동 임계 조합]")
    combos = [
        (-3.0, 1.0), (-3.0, 2.0),
        (-3.5, 1.0), (-3.5, 2.0),
        (-4.0, 1.0), (-4.0, 2.0), (-4.0, 3.0),
        (-4.5, 2.0), (-4.5, 3.0),
        (-5.0, 2.0), (-5.0, 3.0),
    ]
    for t_pct, act_pct in combos:
        _run(f"  trailing {t_pct:+.1f}%  activate≥+{act_pct:.0f}%",
             _make_strategy(buy_threshold=0.50, sell_threshold=-0.40,
                            trailing_pct=t_pct, activate_profit_pct=act_pct), df)

    print("\n★ = 수익률 >+23% AND MDD <13% (기준선 대비 개선)")


if __name__ == "__main__":
    main()
