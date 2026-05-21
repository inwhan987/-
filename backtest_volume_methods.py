"""거래량 분석 방법 비교 백테스트.

비교 대상:
  1. OFF          — 거래량 필터 없음 (기준)
  2. MA(25)       — 현재 방식: 현재 거래량 vs 25봉 평균 (cfg 내장)
  3. 거래대금      — 거래량×가격 기반 MA 비교
  4. OBV 추세     — OBV > OBV_MA(25) 이면 boost
  5. VROC(10)     — 거래량 변화율 기반
  6. 시간대 정규화 — 같은 분(minute)의 과거 평균과 비교

사용:
  python backtest_volume_methods.py [symbol] [period]
  python backtest_volume_methods.py 005930.KS 60d
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd

from backtest_current import _load_env, _download, ATR_STOP_MAX_PCT
from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.strategy.ma_cross import MACrossSignal
from stock_bot.indicators.atr import atr_from_ohlcv

BOOST   = 0.10
PENALTY = 0.05


def _base_cfg(env: dict) -> EnsembleConfig:
    def _g(key, default, cast=float):
        try:
            return cast(env[key]) if key in env else default
        except Exception:
            return default

    cfg = EnsembleConfig()
    cfg.vwap_band              = _g("TRADE_VWAP_BAND",              0.008)
    cfg.vwap_sell_band         = _g("TRADE_VWAP_SELL_BAND",         0.0085) or None
    cfg.vwap_st_bull_sell_band = _g("TRADE_VWAP_ST_BULL_SELL_BAND", 0.009) or None
    cfg.vwap_warmup_bars       = _g("TRADE_VWAP_WARMUP_BARS",       8, int)
    cfg.rsi_period             = _g("TRADE_RSI_PERIOD",             20, int)
    cfg.rsi_oversold           = _g("TRADE_RSI_OVERSOLD",           30.0)
    cfg.rsi_overbought         = _g("TRADE_RSI_OVERBOUGHT",         74.0)
    cfg.supertrend_period      = _g("TRADE_SUPERTREND_PERIOD",      7, int)
    cfg.supertrend_mult        = _g("TRADE_SUPERTREND_MULT",        3.0)
    cfg.bb_window              = _g("TRADE_BB_WINDOW",              20, int)
    cfg.bb_k                   = _g("TRADE_BB_K",                   2.0)
    cfg.bb_consec              = _g("TRADE_BB_CONSEC",              3, int)
    raw_w = env.get("ENSEMBLE_WEIGHTS", "0.225,0.225,0.225,0.225,0.10")
    try:
        cfg.weights = tuple(float(x) for x in raw_w.split(","))
    except Exception:
        cfg.weights = (0.225, 0.225, 0.225, 0.225, 0.10)
    cfg.min_buy_votes  = _g("ENSEMBLE_MIN_BUY_VOTES",  2, int)
    cfg.buy_threshold  = _g("ENSEMBLE_BUY_THRESHOLD",  0.5)
    cfg.min_sell_votes = _g("ENSEMBLE_MIN_SELL_VOTES", 2, int)
    cfg.sell_threshold = _g("ENSEMBLE_SELL_THRESHOLD", -0.55)

    cfg.daily_context_profit_gate_pct = _g("DAILY_CONTEXT_PROFIT_GATE_PCT", 1.5)
    cfg.daily_context_avwap_pct       = _g("DAILY_CONTEXT_AVWAP_PCT",       1.5)
    cfg.daily_context_pdh_pct         = _g("DAILY_CONTEXT_PDH_PCT",         1.0)
    cfg.daily_context_pdc_pct         = _g("DAILY_CONTEXT_PDC_PCT",         2.0)
    cfg.daily_context_trend_bonus     = _g("DAILY_CONTEXT_TREND_BONUS",     0.5)

    cfg.volume_filter_enabled = False
    cfg.volume_ma_period      = _g("ENSEMBLE_VOLUME_MA_PERIOD",    25, int)
    cfg.volume_high_ratio     = _g("ENSEMBLE_VOLUME_HIGH_RATIO",   1.2)
    cfg.volume_low_ratio      = _g("ENSEMBLE_VOLUME_LOW_RATIO",    0.7)
    cfg.volume_score_boost    = _g("ENSEMBLE_VOLUME_SCORE_BOOST",  0.10)
    cfg.volume_score_penalty  = _g("ENSEMBLE_VOLUME_SCORE_PENALTY",0.05)
    return cfg


# ── 거래량 신호 계산 함수들 ────────────────────────────────────────────────────
# 반환: (ratio, is_high, is_low)
#   ratio   = 현재/평균
#   is_high = True if 거래량 많음
#   is_low  = True if 거래량 적음

def _vol_signal_ma(df: pd.DataFrame, ma_period: int = 25):
    """현재 방식: MA(ma_period) 비교."""
    if "volume" not in df.columns or len(df) < ma_period + 1:
        return None
    vol = df["volume"]
    avg = float(vol.rolling(ma_period).mean().iloc[-1])
    cur = float(vol.iloc[-1])
    if avg <= 0:
        return None
    ratio = cur / avg
    return ratio, ratio >= 1.2, ratio <= 0.7


def _vol_signal_turnover(df: pd.DataFrame, ma_period: int = 25):
    """거래대금(거래량×가격) MA(ma_period) 비교."""
    if "volume" not in df.columns or "close" not in df.columns or len(df) < ma_period + 1:
        return None
    to = df["volume"] * df["close"]
    avg = float(to.rolling(ma_period).mean().iloc[-1])
    cur = float(to.iloc[-1])
    if avg <= 0:
        return None
    ratio = cur / avg
    return ratio, ratio >= 1.2, ratio <= 0.7


def _vol_signal_obv(df: pd.DataFrame, ma_period: int = 25):
    """OBV 추세: OBV > OBV_MA(ma_period) → high, 반대 → low."""
    if "volume" not in df.columns or "close" not in df.columns or len(df) < ma_period + 2:
        return None
    closes = df["close"].values
    vols   = df["volume"].values
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv[i] = obv[i-1] + vols[i]
        elif closes[i] < closes[i-1]:
            obv[i] = obv[i-1] - vols[i]
        else:
            obv[i] = obv[i-1]
    obv_s  = pd.Series(obv)
    obv_ma = float(obv_s.rolling(ma_period).mean().iloc[-1])
    cur_obv = float(obv_s.iloc[-1])
    # OBV가 MA 위 = 매집 = 거래량 "많음"으로 해석
    ratio = cur_obv / abs(obv_ma) if obv_ma != 0 else 1.0
    is_high = cur_obv > obv_ma
    is_low  = cur_obv < obv_ma
    return ratio, is_high, is_low


def _vol_signal_vroc(df: pd.DataFrame, ma_period: int = 25):
    """VROC(10): 거래량 변화율."""
    N = 10
    if "volume" not in df.columns or len(df) < N + 2:
        return None
    vol = df["volume"]
    prev = float(vol.iloc[-N-1])
    cur  = float(vol.iloc[-1])
    if prev <= 0:
        return None
    vroc = (cur - prev) / prev
    ratio = 1.0 + vroc
    return ratio, vroc >= 0.2, vroc <= -0.2


def _vol_signal_time_norm(df: pd.DataFrame, ma_period: int = 25):
    """시간대 정규화: 같은 시간대 과거 평균과 비교."""
    if "volume" not in df.columns or len(df) < 2:
        return None
    if not hasattr(df.index, 'minute'):
        return None
    cur_minute = df.index[-1].minute
    cur_hour   = df.index[-1].hour
    same_slot = df[(df.index.hour == cur_hour) & (df.index.minute == cur_minute)]["volume"]
    if len(same_slot) < 3:
        return None
    avg = float(same_slot.iloc[:-1].mean())
    cur = float(same_slot.iloc[-1])
    if avg <= 0:
        return None
    ratio = cur / avg
    return ratio, ratio >= 1.2, ratio <= 0.7


# ── 전략 함수 생성 ─────────────────────────────────────────────────────────────

def _make_strategy(env, vol_signal_fn=None):
    ma_period = int(env.get("ENSEMBLE_VOLUME_MA_PERIOD", 25))

    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        cfg = _base_cfg(env)

        if ctx:
            cfg.daily_context_entry_date     = ctx.get("entry_date")
            cfg.daily_context_prev_day_high  = ctx.get("prev_day_high", 0.0)
            cfg.daily_context_prev_day_close = ctx.get("prev_day_close", 0.0)

        last_price = float(df_slice["close"].iloc[-1])
        ohlcv_list = [
            {"open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume}
            for r in df_slice.itertuples()
        ]
        atr_val = atr_from_ohlcv(ohlcv_list, period=14)
        if atr_val and atr_val > 0:
            stop_pct = min((atr_val * 12.0) / last_price * 100, ATR_STOP_MAX_PCT)
        else:
            stop_pct = ATR_STOP_MAX_PCT

        today_date = df_slice.index[-1].date()
        df_today   = df_slice[df_slice.index.date == today_date]

        # MA(25)는 cfg 내장 필터 사용
        if vol_signal_fn == "ma":
            cfg.volume_filter_enabled = True
            decision = decide_ensemble(
                df_slice["close"], ohlcv_df=df_today, ohlcv_df_hist=df_slice,
                position_qty=position_qty, avg_price=avg_price,
                stop_loss_pct=stop_pct, config=cfg,
            )
            return decision.signal.value

        # 기타 방법: OFF 결정을 기반으로 거래량 신호로 필터링
        cfg.volume_filter_enabled = False
        decision = decide_ensemble(
            df_slice["close"], ohlcv_df=df_today, ohlcv_df_hist=df_slice,
            position_qty=position_qty, avg_price=avg_price,
            stop_loss_pct=stop_pct, config=cfg,
        )
        base_signal = decision.signal.value

        if vol_signal_fn is None:
            return base_signal

        # 거래량 신호 계산 (ma_period는 env에서 읽어 바인딩)
        sig = vol_signal_fn(df_slice, ma_period)
        if sig is None:
            return base_signal

        _, is_high, is_low = sig

        # 거래량 기반 필터:
        #   BUY  + 거래량 낮음 → HOLD  (가짜 돌파 차단)
        #   BUY  + 거래량 높음 → BUY 유지
        #   SELL + 거래량 높음 → HOLD  (강한 매수세로 반등 가능성)
        #   나머지              → 기본 신호 유지
        if base_signal == MACrossSignal.BUY.value and is_low:
            return MACrossSignal.HOLD.value
        if base_signal == MACrossSignal.SELL.value and is_high:
            return MACrossSignal.HOLD.value
        return base_signal

    return _fn


def _run(df, fn, symbol, env):
    return run_strategy(
        df, fn, symbol,
        stop_loss_pct              = ATR_STOP_MAX_PCT,
        enable_add_buy             = False,
        post_stoploss_cooldown_min = int(env.get("POST_STOPLOSS_COOLDOWN_MIN", "30")),
        initial_position_fraction  = float(env.get("POSITION_FRACTION", "0.70")),
        bar_minutes                = 5,
        sell_on_next_open          = True,
    )


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"

    print(f"데이터 다운로드... {symbol} {period}")
    df = _download(symbol, period)
    print(f"봉 수: {len(df)}\n")

    env = _load_env()

    methods = [
        ("OFF (기준)",      None),
        ("MA(25) 내장",     "ma"),
        ("거래대금 MA(25)", _vol_signal_turnover),
        ("OBV 추세",        _vol_signal_obv),
        ("VROC(10)",        _vol_signal_vroc),
        ("시간대 정규화",   _vol_signal_time_norm),
    ]

    results = []
    for label, vol_fn in methods:
        fn = _make_strategy(env, vol_fn)
        r  = _run(df, fn, symbol, env)
        results.append((label, r))

    print(f"\n{'방법':<18} {'거래':>4} {'수익률':>9} {'승률':>7} {'MDD':>8}  vs OFF")
    print("─" * 62)
    base_ret = None
    for label, r in results:
        ret  = r.total_return_pct
        wr   = r.win_rate
        mdd  = r.max_drawdown_pct
        cnt  = r.trades
        if base_ret is None:
            base_ret = ret
            diff_str = "← 기준"
        else:
            diff = ret - base_ret
            diff_str = f"({diff:+.2f}%p)"
        print(f"{label:<18} {cnt:>4}   {ret:>+7.2f}%  {wr:>5.1f}%  {mdd:>6.2f}%  {diff_str}")


if __name__ == "__main__":
    main()
