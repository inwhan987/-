"""Tier 1/2/3 개선안 종합 백테스트.

기준선: 현재 실전 설정 (POSITION_FRACTION 0.70, ADD_BUY OFF)

테스트 항목:
  Tier 1: 임계값 (BUY/SELL), 트레일링 스톱
  Tier 2: 시간대 필터, 거래량 임계값
  Tier 3: ADX 동적 임계값, 가중치 변형
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


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm[(plus_dm - minus_dm) < 0] = 0
    minus_dm[(minus_dm - plus_dm) < 0] = 0
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=1).mean()
    plus_di = 100 * plus_dm.rolling(period, min_periods=1).mean() / atr
    minus_di = 100 * minus_dm.rolling(period, min_periods=1).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(period, min_periods=1).mean()


def _make_strategy(
    *,
    buy_threshold: float = 0.40,
    sell_threshold: float = -0.30,
    weights: tuple = (0.25, 0.22, 0.20, 0.18, 0.15),
    volume_high: float = 1.2,
    volume_low: float = 0.7,
    volume_penalty: float = 0.05,
    trailing_pct: float = 0.0,           # 0이면 비활성 (음수면 발동)
    trailing_profit_only: bool = True,
    closing_block_start: str | None = None,  # "14:50" 등
    closing_block_end: str | None = None,
    adx_strict_threshold: float = 0.0,       # ADX 이 값 미만이면 buy_threshold +0.05
    adx_loose_threshold: float = 0.0,        # ADX 이 값 이상이면 buy_threshold -0.05 (강한추세 완화)
    adx_series: pd.Series | None = None,
    lunch_block: bool = False,               # 12:00~13:00 점심시간 매수 차단
):
    env = _load_env()
    def _g(key, default, cast=float):
        try:
            return cast(env[key]) if key in env else default
        except Exception:
            return default

    state = {"max_since_entry": 0.0, "last_qty": 0}

    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        last_price = float(df_slice["close"].iloc[-1])
        now = df_slice.index[-1]
        today_date = now.date()
        now_time = now.time()

        # 상태
        if position_qty > 0 and state["last_qty"] == 0:
            state["max_since_entry"] = last_price
        elif position_qty == 0:
            state["max_since_entry"] = 0.0
        state["last_qty"] = position_qty
        if position_qty > 0:
            state["max_since_entry"] = max(state["max_since_entry"], last_price)

        # 트레일링 (수익권만)
        if trailing_pct < 0 and position_qty > 0 and state["max_since_entry"] > 0:
            if not (trailing_profit_only and avg_price > 0 and last_price <= avg_price):
                drop = (last_price / state["max_since_entry"] - 1) * 100
                if drop <= trailing_pct:
                    return "sell"

        # 기본 cfg
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

        # ADX 동적 buy_threshold (양방향)
        eff_buy_th = buy_threshold
        if adx_series is not None:
            try:
                cur_adx = adx_series.loc[now]
                if pd.notna(cur_adx):
                    if adx_strict_threshold > 0 and cur_adx < adx_strict_threshold:
                        eff_buy_th += 0.05   # 횡보: 임계값 강화
                    elif adx_loose_threshold > 0 and cur_adx >= adx_loose_threshold:
                        eff_buy_th -= 0.05   # 강한추세: 임계값 완화
            except (KeyError, ValueError):
                pass

        cfg.buy_threshold               = eff_buy_th
        cfg.add_buy_threshold           = _g("ADD_BUY_THRESHOLD",            0.45)
        cfg.add_buy_min_votes           = _g("ADD_BUY_MIN_VOTES",            2, int)
        cfg.min_sell_votes              = _g("ENSEMBLE_MIN_SELL_VOTES",      2, int)
        cfg.sell_threshold              = sell_threshold
        cfg.volume_filter_enabled       = env.get("ENSEMBLE_VOLUME_FILTER_ENABLED", "true").lower() == "true"
        cfg.volume_high_ratio           = volume_high
        cfg.volume_low_ratio            = volume_low
        cfg.volume_score_boost          = _g("ENSEMBLE_VOLUME_SCORE_BOOST",  0.10)
        cfg.volume_score_penalty        = volume_penalty
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

        # 마감 직전 매수 차단
        if sig == "buy" and closing_block_start and closing_block_end:
            from datetime import time as dt_time
            hh1, mm1 = map(int, closing_block_start.split(":"))
            hh2, mm2 = map(int, closing_block_end.split(":"))
            if dt_time(hh1, mm1) <= now_time <= dt_time(hh2, mm2):
                return "hold"

        # 점심시간 매수 차단 (12:00~13:00)
        if sig == "buy" and lunch_block:
            from datetime import time as dt_time
            if dt_time(12, 0) <= now_time <= dt_time(13, 0):
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


def _run(label: str, fn, df: pd.DataFrame, frac: float = 0.70,
         take_profit_levels=None) -> tuple:
    r = run_strategy(
        df, fn, "test", stop_loss_pct=5.0,
        enable_add_buy=False,
        initial_position_fraction=frac,
        bar_minutes=5,
        take_profit_levels=take_profit_levels,
    )
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    print(f"{label:<42} {r.total_return_pct:>+7.2f}% {r.trades:>4} {r.win_rate:>5.1f}% "
          f"{r.max_drawdown_pct:>5.1f}% {r.sharpe:>5.2f} {pf:>5}")
    return r.total_return_pct, r.win_rate, r.max_drawdown_pct, r.sharpe


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"

    print(f"종목: {symbol}  기간: {period}")
    print("다운로드 중...", end=" ", flush=True)
    df = _download(symbol, period)
    adx = _compute_adx(df)
    print(f"{len(df)}봉\n")

    hdr = f"{'설정':<42} {'수익률':>7} {'거래':>4} {'승률':>6} {'MDD':>6} {'샤프':>5} {'손익비':>5}"
    print(hdr)
    print("=" * len(hdr))

    # ── 기준선 ───────────────────────────────────────────────
    print("\n[기준선]")
    base = _run("Baseline (현재: BUY 0.40, SELL -0.30)",
                _make_strategy(buy_threshold=0.40, sell_threshold=-0.30), df)

    # ── Tier 1: 임계값 ────────────────────────────────────────
    print("\n[Tier 1: BUY 임계값]")
    _run("BUY 0.42",  _make_strategy(buy_threshold=0.42), df)
    _run("BUY 0.45",  _make_strategy(buy_threshold=0.45), df)
    _run("BUY 0.50",  _make_strategy(buy_threshold=0.50), df)

    print("\n[Tier 1: SELL 임계값]")
    _run("SELL -0.25 (빠른 매도)", _make_strategy(sell_threshold=-0.25), df)
    _run("SELL -0.35 (느린 매도)", _make_strategy(sell_threshold=-0.35), df)
    _run("SELL -0.40 (더 느린)",  _make_strategy(sell_threshold=-0.40), df)

    print("\n[Tier 1: 트레일링 (수익권만)]")
    _run("트레일링 -3%",  _make_strategy(trailing_pct=-3.0), df)
    _run("트레일링 -4%",  _make_strategy(trailing_pct=-4.0), df)
    _run("트레일링 -5%",  _make_strategy(trailing_pct=-5.0), df)

    # ── Tier 2: 시간대, 거래량 ───────────────────────────────────
    print("\n[Tier 2: 시간대 필터]")
    _run("14:50~15:20 매수 차단", _make_strategy(closing_block_start="14:50", closing_block_end="15:20"), df)
    _run("14:30~15:20 매수 차단", _make_strategy(closing_block_start="14:30", closing_block_end="15:20"), df)

    print("\n[Tier 2: 거래량 임계값 강화]")
    _run("거래량 엄격 (1.5/0.5)",   _make_strategy(volume_high=1.5, volume_low=0.5), df)
    _run("거래량 더엄격 (1.8/0.4)", _make_strategy(volume_high=1.8, volume_low=0.4), df)
    _run("거래량 페널티 -0.10",     _make_strategy(volume_penalty=0.10), df)

    # ── Tier 3: ADX, 가중치 ──────────────────────────────────
    print("\n[Tier 2: 점심시간 필터]")
    _run("점심 차단 12:00~13:00",       _make_strategy(lunch_block=True), df)
    _run("점심+마감 차단",              _make_strategy(lunch_block=True,
                                                      closing_block_start="14:50",
                                                      closing_block_end="15:20"), df)

    print("\n[Tier 2: 분할 익절]")
    base_fn = _make_strategy(buy_threshold=0.50, sell_threshold=-0.40)
    _run("분할익절 +3%→30%, +5%→30%",
         base_fn, df, take_profit_levels=[(3.0, 0.30), (5.0, 0.30)])
    _run("분할익절 +2%→30%, +4%→30%",
         base_fn, df, take_profit_levels=[(2.0, 0.30), (4.0, 0.30)])
    _run("분할익절 +3%→50%",
         base_fn, df, take_profit_levels=[(3.0, 0.50)])
    _run("분할익절 +5%→50%",
         base_fn, df, take_profit_levels=[(5.0, 0.50)])

    print("\n[Tier 3: ADX 동적 임계값]")
    _run("ADX < 20 시 BUY +0.05 (횡보 강화)",
         _make_strategy(adx_strict_threshold=20.0, adx_series=adx), df)
    _run("ADX < 25 시 BUY +0.05 (횡보 강화)",
         _make_strategy(adx_strict_threshold=25.0, adx_series=adx), df)
    _run("ADX ≥ 30 시 BUY -0.05 (강추세 완화)",
         _make_strategy(adx_loose_threshold=30.0, adx_series=adx), df)
    _run("ADX ≥ 25 시 BUY -0.05 (강추세 완화)",
         _make_strategy(adx_loose_threshold=25.0, adx_series=adx), df)
    _run("ADX 양방향 (<20+0.05 / ≥30-0.05)",
         _make_strategy(adx_strict_threshold=20.0, adx_loose_threshold=30.0,
                        adx_series=adx), df)

    print("\n[Tier 3: 가중치 변형]")
    _run("ST 강화 (0.20/0.30/0.20/0.15/0.15)", _make_strategy(weights=(0.20, 0.30, 0.20, 0.15, 0.15)), df)
    _run("VWAP 강화 (0.35/0.20/0.18/0.17/0.10)", _make_strategy(weights=(0.35, 0.20, 0.18, 0.17, 0.10)), df)
    _run("RSI 강화 (0.20/0.20/0.30/0.15/0.15)",  _make_strategy(weights=(0.20, 0.20, 0.30, 0.15, 0.15)), df)
    _run("균등 (0.20×5)",                        _make_strategy(weights=(0.20, 0.20, 0.20, 0.20, 0.20)), df)
    _run("BB 강화 (0.20/0.20/0.20/0.25/0.15)",   _make_strategy(weights=(0.20, 0.20, 0.20, 0.25, 0.15)), df)

    # ── 조합 시도 (베스트들 합치기) ─────────────────────────────
    print("\n[조합 - 베스트 후보들]")
    _run("BUY 0.45 + 트레일링 -4%", _make_strategy(buy_threshold=0.45, trailing_pct=-4.0), df)
    _run("BUY 0.45 + SELL -0.35",   _make_strategy(buy_threshold=0.45, sell_threshold=-0.35), df)
    _run("BUY 0.45 + 거래량엄격",    _make_strategy(buy_threshold=0.45, volume_high=1.5, volume_low=0.5), df)
    _run("BUY 0.45 + ADX25 + 트레일링", _make_strategy(buy_threshold=0.45, adx_strict_threshold=25.0,
                                                       adx_series=adx, trailing_pct=-4.0), df)
    _run("올인원 (BUY 0.45/SELL -0.35/트레일링 -4%/14:30 차단)",
         _make_strategy(buy_threshold=0.45, sell_threshold=-0.35, trailing_pct=-4.0,
                         closing_block_start="14:30", closing_block_end="15:20"), df)


if __name__ == "__main__":
    main()
