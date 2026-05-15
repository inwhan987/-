"""삼성전자 단일 종목으로 속도/트레일링 임계값 그리드 테스트.

매개변수:
  velocity_bars × velocity_pct × trailing_pct × trailing_profit_only

사용: python backtest_velocity_grid.py [symbol] [period]
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


def _make_strategy(velocity_bars: int, velocity_pct: float,
                   trailing_pct: float, trailing_profit_only: bool):
    """
    velocity_pct: 음수 (예: -2.0). 0이면 비활성.
    trailing_pct: 음수 (예: -2.0). 0이면 비활성.
    trailing_profit_only: True면 평단보다 위(수익권)일 때만 트레일링 발동.
    """
    env = _load_env()
    def _g(key, default, cast=float):
        try:
            return cast(env[key]) if key in env else default
        except Exception:
            return default

    state = {"max_since_entry": 0.0, "last_qty": 0}

    def _fn(df_slice, position_qty, avg_price, stop_loss_pct, ctx=None):
        last_price = float(df_slice["close"].iloc[-1])
        today_date = df_slice.index[-1].date()

        if position_qty > 0 and state["last_qty"] == 0:
            state["max_since_entry"] = last_price
        elif position_qty == 0:
            state["max_since_entry"] = 0.0
        state["last_qty"] = position_qty

        if position_qty > 0:
            state["max_since_entry"] = max(state["max_since_entry"], last_price)

        # 속도 매도
        if velocity_pct < 0 and position_qty > 0:
            n_bars = velocity_bars + 1
            if len(df_slice) >= n_bars:
                start_price = float(df_slice["close"].iloc[-n_bars])
                drop_pct = (last_price / start_price - 1) * 100
                if drop_pct <= velocity_pct:
                    return "sell"

        # 트레일링
        if trailing_pct < 0 and position_qty > 0 and state["max_since_entry"] > 0:
            # profit_only: 평단보다 위에 있을 때만
            if trailing_profit_only and avg_price > 0 and last_price <= avg_price:
                pass  # skip
            else:
                drop_from_high = (last_price / state["max_since_entry"] - 1) * 100
                if drop_from_high <= trailing_pct:
                    return "sell"

        # 기존 전략
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


def _run_multi(symbols: list[str], dfs: dict) -> None:
    """우승 후보 4가지를 다종목으로 비교."""
    # (라벨, vbars, vpct, tpct, profonly)
    combos = [
        ("A. 현재 (기준)",                       0, 0,    0,    False),
        ("B6. 속도 -4% (20분)",                  4, -4.0, 0,    False),
        ("C4. 트레일링 -3% (수익권만)",          0, 0,    -3.0, True),
        ("C5. 트레일링 -4% (수익권만)",          0, 0,    -4.0, True),
        ("D3. 속도-4%(20분) + 트레일링-3% 수익권", 4, -4.0, -3.0, True),
    ]

    all_rows = {}  # label -> {symbol -> (ret, mdd, trades, winrate)}
    avg_rows = []
    for label, vbars, vpct, tpct, profonly in combos:
        rows = {}
        for s in symbols:
            if s not in dfs:
                continue
            fn = _make_strategy(vbars, vpct, tpct, profonly)
            try:
                r = run_strategy(dfs[s], fn, s, stop_loss_pct=5.0)
                rows[s] = (r.total_return_pct, r.max_drawdown_pct,
                           r.trades, r.win_rate)
            except Exception as e:
                rows[s] = (0, 0, 0, 0)
        all_rows[label] = rows
        rets = [v[0] for v in rows.values()]
        mdds = [v[1] for v in rows.values()]
        avg_rows.append((label, sum(rets)/len(rets) if rets else 0,
                         sum(mdds)/len(mdds) if mdds else 0))

    # 종목별 표
    print("\n[종목별 수익률]")
    hdr = f"{'설정':<38}" + "".join(f"{s.split('.')[0]:>9}" for s in symbols) + f"{'평균':>9}"
    print(hdr)
    print("-" * len(hdr))
    for label, rows in all_rows.items():
        line = f"{label:<38}"
        rets = []
        for s in symbols:
            r = rows.get(s, (0,))[0]
            rets.append(r)
            line += f"{r:>+8.2f}%"
        avg = sum(rets) / len(rets) if rets else 0
        line += f"{avg:>+8.2f}%"
        print(line)

    # 요약 (수익률·MDD·기준선 대비 차이)
    base_ret, base_mdd = avg_rows[0][1], avg_rows[0][2]
    print(f"\n{'설정':<38} {'평균수익률':>10} {'평균MDD':>9} {'수익차':>10} {'MDD차':>9}")
    print("-" * 90)
    for label, avg_ret, avg_mdd in avg_rows:
        diff_ret = avg_ret - base_ret if label != avg_rows[0][0] else 0
        diff_mdd = avg_mdd - base_mdd if label != avg_rows[0][0] else 0
        if label == avg_rows[0][0]:
            print(f"{label:<38} {avg_ret:>+9.2f}% {avg_mdd:>8.1f}% {'-':>10} {'-':>9}")
        else:
            print(f"{label:<38} {avg_ret:>+9.2f}% {avg_mdd:>8.1f}% {diff_ret:>+9.2f}%p {diff_mdd:>+8.1f}%p")


def main():
    # 다종목 지원: 쉼표 구분
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols = [s.strip() for s in symbols_str.split(",")]

    # 단일 종목이면 기존 동작, 다종목이면 다종목 모드
    if len(symbols) == 1:
        symbol = symbols[0]
        print(f"종목: {symbol}  기간: {period}")
        print("다운로드 중...", end=" ", flush=True)
        df = _download(symbol, period)
        print(f"{len(df)}봉")
    else:
        print(f"종목: {len(symbols)}개  기간: {period}")
        dfs = {}
        for s in symbols:
            print(f"  {s}...", end=" ", flush=True)
            try:
                dfs[s] = _download(s, period)
                print(f"{len(dfs[s])}봉")
            except Exception as e:
                print(f"실패: {e}")
        # 다종목 모드: 단일 함수 분기로 처리
        _run_multi(symbols, dfs)
        return

    # 테스트 조합 정의
    # (라벨, velocity_bars, velocity_pct, trailing_pct, profit_only)
    combos = [
        # 기준선
        ("A. 현재 (기준)",             0, 0,    0,    False),

        # 속도 단독
        ("B1. 속도 -2% (10분)",        2, -2.0, 0,    False),
        ("B2. 속도 -2.5% (10분)",      2, -2.5, 0,    False),
        ("B3. 속도 -3% (10분)",        2, -3.0, 0,    False),
        ("B4. 속도 -3% (15분)",        3, -3.0, 0,    False),
        ("B5. 속도 -4% (15분)",        3, -4.0, 0,    False),
        ("B6. 속도 -4% (20분)",        4, -4.0, 0,    False),

        # 트레일링 단독
        ("C1. 트레일링 -2%",           0, 0,    -2.0, False),
        ("C2. 트레일링 -3%",           0, 0,    -3.0, False),
        ("C3. 트레일링 -4%",           0, 0,    -4.0, False),
        ("C4. 트레일링 -3% (수익권만)", 0, 0,    -3.0, True),
        ("C5. 트레일링 -2% (수익권만)", 0, 0,    -2.0, True),

        # 조합
        ("D1. 속도-3% + 트레일링-3% 수익권", 2, -3.0, -3.0, True),
        ("D2. 속도-3% + 트레일링-4%",         2, -3.0, -4.0, False),
        ("D3. 속도-4%(15분) + 트레일링-3% 수익권", 3, -4.0, -3.0, True),
    ]

    results = []
    for label, vbars, vpct, tpct, profonly in combos:
        fn = _make_strategy(vbars, vpct, tpct, profonly)
        try:
            r = run_strategy(df, fn, symbol, stop_loss_pct=5.0)
            results.append((label, r.total_return_pct, r.trades, r.win_rate,
                            r.max_drawdown_pct, r.sharpe, r.profit_factor))
        except Exception as e:
            results.append((label, 0, 0, 0, 0, 0, 0))

    # 출력
    print(f"\n{'설정':<40} {'수익률':>8} {'거래':>5} {'승률':>6} {'MDD':>6} {'샤프':>6} {'손익비':>6}")
    print("-" * 90)
    for label, ret, trades, winrate, mdd, sharpe, pf in results:
        pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
        print(f"{label:<40} {ret:>+7.2f}% {trades:>5} {winrate:>5.1f}% "
              f"{mdd:>5.1f}% {sharpe:>6.2f} {pf_str:>6}")

    # 기준선 대비 차이
    base_ret = results[0][1]
    base_mdd = results[0][4]
    print(f"\n{'설정':<40} {'수익차':>8} {'MDD차':>7}")
    print("-" * 60)
    for label, ret, _, _, mdd, _, _ in results[1:]:
        print(f"{label:<40} {ret - base_ret:>+7.2f}%p {mdd - base_mdd:>+6.1f}%p")


if __name__ == "__main__":
    main()
