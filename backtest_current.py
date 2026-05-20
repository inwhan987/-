"""현재 적용 설정 기준 백테스트.

사용:
  python backtest_current.py [symbol1,symbol2,...] [period]
  예) python backtest_current.py 005930.KS,035720.KS,000660.KS 60d
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
from pathlib import Path

# Windows 한글 경로에서 SSL 인증서 문제 우회
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
    """.env.overrides 읽기 (인라인 주석 제거)."""
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


# 모듈 임포트 시점에 .env.overrides 의 ATR_STOP_MAX_PCT 를 읽어 상수로 노출.
# (다수의 backtest_*.py 가 이 상수를 import 하므로 호환 유지)
try:
    ATR_STOP_MAX_PCT = float(_load_env().get("ATR_STOP_MAX_PCT", "5.0"))
except Exception:
    ATR_STOP_MAX_PCT = 5.0


def _make_current():
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
        cfg.bb_window                   = _g("TRADE_BB_WINDOW",  20, int)
        cfg.bb_k                        = _g("TRADE_BB_K",       2.0)
        cfg.bb_consec                   = _g("TRADE_BB_CONSEC",  3, int)
        # 가중치
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
        cfg.daily_context_profit_gate_pct = _g("DAILY_CONTEXT_PROFIT_GATE_PCT", 1.5)
        cfg.daily_context_avwap_pct       = _g("DAILY_CONTEXT_AVWAP_PCT",       1.5)
        cfg.daily_context_pdh_pct         = _g("DAILY_CONTEXT_PDH_PCT",         1.0)
        cfg.daily_context_pdc_pct         = _g("DAILY_CONTEXT_PDC_PCT",         1.5)
        cfg.daily_context_trend_bonus     = _g("DAILY_CONTEXT_TREND_BONUS",     0.5)
        # MACD 6번째 전략
        cfg.macd_enabled                  = env.get("ENSEMBLE_MACD_ENABLED", "false").lower() == "true"
        cfg.macd_weight                   = _g("ENSEMBLE_MACD_WEIGHT",         0.225)
        cfg.macd_fast                     = _g("ENSEMBLE_MACD_FAST",           12, int)
        cfg.macd_slow                     = _g("ENSEMBLE_MACD_SLOW",           26, int)
        cfg.macd_signal_period            = _g("ENSEMBLE_MACD_SIGNAL",         9, int)
        # S/R 7번째 전략
        cfg.sr_enabled                    = env.get("ENSEMBLE_SR_ENABLED", "false").lower() == "true"
        cfg.sr_weight                     = _g("ENSEMBLE_SR_WEIGHT",           0.15)
        cfg.sr_lookback                   = _g("ENSEMBLE_SR_LOOKBACK",         60, int)
        cfg.sr_swing_window               = _g("ENSEMBLE_SR_SWING_WINDOW",     3, int)
        cfg.sr_proximity_pct              = _g("ENSEMBLE_SR_PROXIMITY_PCT",    0.010)
        # Parabolic SAR 8번째 전략
        cfg.psar_enabled                  = env.get("ENSEMBLE_PSAR_ENABLED", "false").lower() == "true"
        cfg.psar_weight                   = _g("ENSEMBLE_PSAR_WEIGHT",         0.15)
        cfg.psar_step                     = _g("ENSEMBLE_PSAR_STEP",           0.02)
        cfg.psar_max_af                   = _g("ENSEMBLE_PSAR_MAX_AF",         0.20)
        cfg.psar_min_bars                 = _g("ENSEMBLE_PSAR_MIN_BARS",       10, int)
        # Classic Pivot Point 9번째 전략
        cfg.pivot_enabled                 = env.get("ENSEMBLE_PIVOT_ENABLED", "false").lower() == "true"
        cfg.pivot_weight                  = _g("ENSEMBLE_PIVOT_WEIGHT",        0.15)
        cfg.pivot_proximity_pct           = _g("ENSEMBLE_PIVOT_PROXIMITY_PCT", 0.005)
        cfg.pivot_breakout_pct            = _g("ENSEMBLE_PIVOT_BREAKOUT_PCT",  0.002)

        # ctx에서 DailyContext 정보 주입
        if ctx:
            cfg.daily_context_entry_date      = ctx.get("entry_date")
            cfg.daily_context_prev_day_high   = ctx.get("prev_day_high", 0.0)
            cfg.daily_context_prev_day_close  = ctx.get("prev_day_close", 0.0)

        # ATR 동적 손절 (캡 5%)
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


def _make_htf_dir(df5m: pd.DataFrame, tf_min: int, adx_period: int, adx_threshold: float = 30.0) -> "pd.Series":
    """5분봉 → HTF 봉 ADX 방향 (1=상승, -1=하락). lookahead 없음.
    ADX > adx_threshold AND -DI > +DI → 하락(-1), 나머지 → 상승(1).
    """
    htf = df5m.resample(f"{tf_min}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])

    hi, lo, cl = htf["high"], htf["low"], htf["close"]
    tr = pd.concat([hi - lo, (hi - cl.shift(1)).abs(), (lo - cl.shift(1)).abs()], axis=1).max(axis=1)
    dm_p_raw = (hi - hi.shift(1)).clip(lower=0)
    dm_m_raw = (lo.shift(1) - lo).clip(lower=0)
    dm_p = dm_p_raw.where(dm_p_raw > dm_m_raw, 0.0)
    dm_m = dm_m_raw.where(dm_m_raw > dm_p_raw, 0.0)

    def wilder(s, n):
        r = s.copy().astype(float) * float("nan")
        r.iloc[n] = s.iloc[1:n+1].sum()
        for i in range(n + 1, len(s)):
            r.iloc[i] = r.iloc[i-1] - r.iloc[i-1] / n + s.iloc[i]
        return r

    n = adx_period
    atr_s = wilder(tr, n)
    dip_s = wilder(dm_p, n)
    dim_s = wilder(dm_m, n)
    di_p = (dip_s / atr_s * 100).replace([float("inf"), float("-inf")], float("nan"))
    di_m = (dim_s / atr_s * 100).replace([float("inf"), float("-inf")], float("nan"))
    dx   = ((di_p - di_m).abs() / (di_p + di_m) * 100).replace([float("inf"), float("-inf")], float("nan"))
    adx  = wilder(dx, n)

    # lookahead 방지: 현재 봉 기준이 아닌 직전 봉 값으로 방향 결정
    adx_s  = adx.shift(2)
    di_p_s = di_p.shift(2)
    di_m_s = di_m.shift(2)

    d = pd.Series(1, index=htf.index)
    d[(adx_s > adx_threshold) & (di_m_s > di_p_s)] = -1
    return d.reindex(df5m.index, method="ffill").fillna(1)


def _wrap_htf(base_fn, htf_dir: "pd.Series", override_enabled: bool,
              override_span: int, override_pct: float):
    """기본 전략 함수를 HTF 블록으로 감쌈."""
    def fn(df_slice, pos, avg, sl, ctx=None):
        sig = base_fn(df_slice, pos, avg, sl, ctx)
        if sig != "buy" or pos != 0:
            return sig
        now = df_slice.index[-1]
        try:
            direction = float(htf_dir.loc[now])
        except Exception:
            direction = 1.0
        if direction > 0:
            return sig
        # 하락추세 → MA 오버라이드 체크
        if override_enabled:
            n = len(df_slice)
            if n >= 20:
                span = override_span if n >= override_span else (override_span // 2 if n >= override_span // 2 else 20)
                ma = float(df_slice["close"].ewm(span=span, adjust=False).mean().iloc[-1])
                cur = float(df_slice["close"].iloc[-1])
                if abs(cur - ma) / ma * 100 <= override_pct:
                    return sig
        return "hold"
    return fn


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
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,035720.KS,000660.KS,005380.KS,035420.KS,068270.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    env = _load_env()
    vb  = float(env.get("TRADE_VWAP_BAND", 0.008)) * 100
    vsb = float(env.get("TRADE_VWAP_SELL_BAND", 0.0085)) * 100
    rp  = env.get("TRADE_RSI_PERIOD", "25")
    sp  = env.get("TRADE_SUPERTREND_PERIOD", "7")
    sm  = env.get("TRADE_SUPERTREND_MULT", "2.5")
    # HTF 블록 설정 (ADX 기반)
    htf_enabled      = env.get("HTF_BLOCK_ENABLED", "false").lower() == "true"
    htf_tf_min       = int(  env.get("HTF_BLOCK_TF_MINUTES",    "30"))
    htf_adx_period   = int(  env.get("HTF_BLOCK_ADX_PERIOD",    "14"))
    htf_adx_thr      = float(env.get("HTF_BLOCK_ADX_THRESHOLD", "30.0"))
    htf_ov_enabled   = env.get("HTF_MA_OVERRIDE_ENABLED", "true").lower() == "true"
    htf_ov_span      = int(  env.get("HTF_MA_OVERRIDE_SPAN",    "120"))
    htf_ov_pct       = float(env.get("HTF_MA_OVERRIDE_PCT",     "1.5"))
    htf_tag = f" | HTF {htf_tf_min}분봉 ADX({htf_adx_period})>{htf_adx_thr:.0f} 차단{'(MA오버라이드ON)' if htf_ov_enabled else ''}" if htf_enabled else ""
    print(f"\n기간: {period}  종목: {', '.join(symbols)}\n")

    # 앙상블 핵심 설정
    weights        = env.get("ENSEMBLE_WEIGHTS", "0.25,0.22,0.20,0.18,0.15")
    buy_thr        = float(env.get("ENSEMBLE_BUY_THRESHOLD",  "0.50"))
    sell_thr       = float(env.get("ENSEMBLE_SELL_THRESHOLD", "-0.40"))
    min_buy_votes  = int(  env.get("ENSEMBLE_MIN_BUY_VOTES",  "2"))
    min_sell_votes = int(  env.get("ENSEMBLE_MIN_SELL_VOTES", "2"))

    # 실전 러너와 동일한 설정 (.env / .env.overrides 에서 로드)
    sell_on_next_open = env.get("SELL_ON_NEXT_OPEN", "true").lower() == "true"
    add_buy_enabled = env.get("ADD_BUY_ENABLED", "true").lower() == "true"
    add_buy_frac    = float(env.get("ADD_BUY_FRACTION",         "0.20"))
    add_buy_max     = int(  env.get("ADD_BUY_MAX_COUNT",        "2"))
    add_buy_maxpos  = float(env.get("ADD_BUY_MAX_POSITION_PCT", "0.80"))
    inherit_stop    = env.get("ADD_BUY_INHERIT_INITIAL_STOP", "true").lower() == "true"
    cooldown_min    = int(  env.get("POST_STOPLOSS_COOLDOWN_MIN", "30"))
    pos_frac        = float(env.get("POSITION_FRACTION",        "0.40"))

    # 거래량 필터
    vol_filt_on     = env.get("ENSEMBLE_VOLUME_FILTER_ENABLED", "true").lower() == "true"
    vol_high        = float(env.get("ENSEMBLE_VOLUME_HIGH_RATIO", "1.2"))
    vol_low         = float(env.get("ENSEMBLE_VOLUME_LOW_RATIO",  "0.7"))

    # DailyContext (오버나이트 청산)
    dc_gate         = float(env.get("DAILY_CONTEXT_PROFIT_GATE_PCT", "1.5"))
    dc_avwap        = float(env.get("DAILY_CONTEXT_AVWAP_PCT",       "1.5"))
    dc_pdh          = float(env.get("DAILY_CONTEXT_PDH_PCT",         "1.0"))
    dc_pdc          = float(env.get("DAILY_CONTEXT_PDC_PCT",         "1.5"))
    dc_bonus        = float(env.get("DAILY_CONTEXT_TREND_BONUS",     "0.5"))
    overnight_thr   = float(env.get("OVERNIGHT_SELL_THRESHOLD",      "-0.20"))
    overnight_votes = int(  env.get("OVERNIGHT_MIN_SELL_VOTES",      "1"))

    # 출력 (한눈에 어떤 설정인지 보이게)
    print("━" * 70)
    print("[전략] 앙상블")
    print(f"  가중치(V/S/R/B/D): {weights}")
    print(f"  BUY  ≥ {buy_thr:+.2f} & {min_buy_votes}표↑  /  SELL ≤ {sell_thr:+.2f} & {min_sell_votes}표↑")
    print()
    rsi_os = env.get("TRADE_RSI_OVERSOLD",   "30")
    rsi_ob = env.get("TRADE_RSI_OVERBOUGHT", "74")
    vwap_warm = env.get("TRADE_VWAP_WARMUP_BARS", "8")
    bb_win  = env.get("TRADE_BB_WINDOW", "20")
    bb_k    = env.get("TRADE_BB_K",      "2.0")
    bb_cs   = env.get("TRADE_BB_CONSEC", "3")
    print("[VWAP/ST/RSI/BB]")
    print(f"  VWAP   매수밴드 {vb:.2f}%  매도밴드 {vsb:.2f}%  워밍업 {vwap_warm}봉")
    print(f"  ST     period={sp}  mult={sm}")
    print(f"  RSI    period={rp}  과매도 {rsi_os}  과매수 {rsi_ob}")
    print(f"  BB     window={bb_win}  k={bb_k}  consec={bb_cs}")
    print()
    print("[거래량 필터]")
    print(f"  enabled={vol_filt_on}  HIGH≥{vol_high}배 boost  LOW≤{vol_low}배 penalty")
    print()
    print("[DailyContext (오버나이트 청산)]")
    print(f"  profit_gate={dc_gate}%  trend_bonus(ST상승)={dc_bonus}%p")
    print(f"  VWAP+{dc_avwap}%  PDH+{dc_pdh}%  PDC+{dc_pdc}%")
    print(f"  override (ST하락+DC SELL): sell_thr={overnight_thr}, min_votes={overnight_votes}")
    print()
    print(f"[HTF 차단]  {('ADX>'+str(htf_adx_thr)+' p='+str(htf_adx_period)+', '+str(htf_tf_min)+'분봉') if htf_enabled else 'OFF'}")
    print()
    print("[손절·포지션]")
    print(f"  ATR 캡 손절 {ATR_STOP_MAX_PCT:.1f}%   손절선 잠금={inherit_stop}   쿨다운={cooldown_min}분")
    print(f"  초기진입 {pos_frac*100:.0f}%   추가매수={add_buy_enabled} (frac={add_buy_frac}, max={add_buy_max}, maxpos={add_buy_maxpos})")
    print(f"  매도 타이밍: {'다음 봉 시가 (실전 동일)' if sell_on_next_open else '현재 봉 종가 즉시 (시뮬용)'}")
    print("━" * 70)
    print()

    hdr = f"{'종목':<14} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7}"
    sep = "=" * len(hdr)
    print(sep)
    print(hdr)
    print("-" * len(hdr))

    base_fn = _make_current()
    total_returns = []

    for symbol in symbols:
        try:
            print(f"  {symbol} 다운로드 중...", end=" ", flush=True)
            df = _download(symbol, period)
            print(f"{len(df)}봉", flush=True)
            # HTF 블록 활성화 시 종목별 방향 계산 후 전략 함수 래핑
            if htf_enabled:
                htf_dir = _make_htf_dir(df, htf_tf_min, htf_adx_period, htf_adx_thr)
                fn = _wrap_htf(base_fn, htf_dir, htf_ov_enabled, htf_ov_span, htf_ov_pct)
            else:
                fn = base_fn
            r = run_strategy(
                df, fn, symbol, stop_loss_pct=ATR_STOP_MAX_PCT,
                enable_add_buy=add_buy_enabled,
                add_buy_fraction=add_buy_frac,
                add_buy_max_count=add_buy_max,
                add_buy_max_position_pct=add_buy_maxpos,
                inherit_initial_stop=inherit_stop,
                post_stoploss_cooldown_min=cooldown_min,
                initial_position_fraction=pos_frac,
                bar_minutes=5,
                sell_on_next_open=sell_on_next_open,
            )
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
            total_returns.append(r.total_return_pct)
        except Exception as e:
            print(f"{symbol:<14} 오류: {e}")

    if total_returns:
        avg = sum(total_returns) / len(total_returns)
        print(sep)
        print(f"{'평균':>14} {avg:>+8.2f}%")
        print(sep)


if __name__ == "__main__":
    main()
