"""파라미터 튜닝 백테스트 비교.

A: 현재 적용 (vwap=0.007, rsi=30/72, min_sell=2, 5분봉)
B: A + min_sell=3 (4번 제안)
C: B + 10분봉    (4+5번 제안)

사용:
  python backtest_tuning.py [symbol] [period]
  예) python backtest_tuning.py 005930.KS 60d
"""
from __future__ import annotations

import sys
import pandas as pd

from stock_bot.backtest.engine import run_strategy, BacktestResult
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.strategy.ma_cross import MACrossSignal


def _make(vwap_band, rsi_os, rsi_ob, min_sell, st_mult=3.0, bb_k=2.0,
          st_period=5, rsi_period=21, weights=None, bb_window=15):
    def _fn(df, position_qty, avg_price, stop_loss_pct):
        cfg = EnsembleConfig()
        cfg.vwap_band          = vwap_band
        cfg.rsi_oversold       = rsi_os
        cfg.rsi_overbought     = rsi_ob
        cfg.min_sell_votes     = min_sell
        cfg.supertrend_mult    = st_mult
        cfg.supertrend_period  = st_period
        cfg.rsi_period         = rsi_period
        cfg.bb_k               = bb_k
        cfg.bb_window          = bb_window
        if weights:
            cfg.weights        = weights
        sig = decide_ensemble(df["close"], df, position_qty, avg_price, stop_loss_pct, cfg)
        return sig.signal.value
    return _fn


def _download(symbol: str, period: str, interval: str) -> pd.DataFrame:
    import yfinance as yf
    # yfinance는 10m 미지원 → 5m 다운 후 리샘플링
    fetch_interval = "5m" if interval == "10m" else interval
    df = yf.download(symbol, period=period, interval=fetch_interval,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"데이터 없음: {symbol} {fetch_interval}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    if interval == "10m":
        df = df.resample("10min").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna(subset=["close"])
    return df


def print_table(results: list[tuple[str, BacktestResult]]) -> None:
    hdr = f"{'케이스':<38} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7}"
    print("\n" + "=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for label, r in results:
        pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
        print(
            f"{label:<38} "
            f"{r.total_return_pct:>+8.2f}% "
            f"{r.trades:>6} "
            f"{r.win_rate:>6.1f}% "
            f"{r.max_drawdown_pct:>6.1f}% "
            f"{r.sharpe:>7.2f} "
            f"{pf:>7}"
        )
    print("=" * len(hdr))


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"
    mode   = sys.argv[3] if len(sys.argv) > 3 else "vwap"

    print(f"\n종목: {symbol}  기간: {period}  모드: {mode}")

    if mode == "bb":
        # BB period/k 스윕 — ST=5, RSI=21, 새 가중치 반영
        # label, bb_window, bb_k
        bb_cases = [
            ("BB 20/2.0 (원래)",   20, 2.0),
            ("BB 15/1.8 (현재적용)", 15, 1.8),
            ("BB 15/1.5",           15, 1.5),
            ("BB 12/1.8",           12, 1.8),
            ("BB 12/1.5",           12, 1.5),
            ("BB 10/1.5",           10, 1.5),
        ]
        W = (0.35, 0.25, 0.25, 0.15)
        results = []
        df_cache: dict[str, pd.DataFrame] = {}
        print("데이터 다운로드 (5m)...", flush=True)
        df_cache["5m"] = _download(symbol, period, "5m")
        df = df_cache["5m"]

        # BB 신호 발생 횟수도 직접 카운트
        from stock_bot.strategy.bollinger import decide_bollinger
        from stock_bot.strategy.ma_cross import MACrossSignal

        print(f"\n{'케이스':<22} {'수익률':>8} {'거래':>5} {'승률':>7} {'샤프':>7}  BB신호횟수")
        print("-" * 65)
        for label, bw, bk in bb_cases:
            fn = _make(0.007, 30.0, 72.0, 2, 3.0, bk, st_period=5, rsi_period=21,
                       weights=W, bb_window=bw)
            r = run_strategy(df, fn, label)
            results.append((label, r))

            # BB 단독 신호 횟수 집계
            bb_signals = 0
            closes = df["close"]
            for i in range(bw + 2, len(closes)):
                d = decide_bollinger(closes.iloc[:i], bw, bk, 0, 0.0)
                if d.signal != MACrossSignal.HOLD:
                    bb_signals += 1

            print(f"  {label:<22} {r.total_return_pct:>+7.2f}%  {r.trades:>4}회  {r.win_rate:>5.1f}%  {r.sharpe:>6.2f}   {bb_signals}회")

        print_table(results)
        return
    elif mode == "vwap_warmup":
        # VWAP 워밍업 봉수 비교 + 진입차단 결합
        from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
        from stock_bot.strategy.ma_cross import MACrossSignal
        from stock_bot.indicators.atr import atr_from_ohlcv
        from stock_bot.backtest.engine import run_strategy
        from datetime import time as _dtime

        print("데이터 다운로드 (5m)...", flush=True)
        df = _download(symbol, period, "5m")

        def _make_warmup(warmup: int, block_end: str = "09:40"):
            block_start = _dtime.fromisoformat("09:00")
            be = _dtime.fromisoformat(block_end)

            def _fn(df_slice, position_qty, avg_price, stop_loss_pct):
                cfg = EnsembleConfig()
                cfg.vwap_band = 0.0085
                cfg.vwap_warmup_bars = warmup
                cfg.rsi_period = 25
                cfg.rsi_oversold = 30.0
                cfg.rsi_overbought = 74.0
                cfg.supertrend_period = 7
                cfg.supertrend_mult = 2.5
                cfg.bb_window = 15
                cfg.bb_k = 1.7
                cfg.bb_consec = 3
                cfg.weights = (0.28, 0.24, 0.16, 0.12, 0.20)
                cfg.volume_filter_enabled = True

                last_price = float(df_slice["close"].iloc[-1])
                ohlcv_list = []
                for ts, row in df_slice.iterrows():
                    ohlcv_list.append({"open": row["open"], "high": row["high"],
                                       "low": row["low"], "close": row["close"], "volume": row["volume"]})
                atr_val = atr_from_ohlcv(ohlcv_list, period=14)
                stop_pct = (atr_val * 12.0) / last_price * 100 if atr_val > 0 else 5.0

                decision = decide_ensemble(df_slice["close"], df_slice, position_qty, avg_price, stop_pct, cfg)

                # 진입 차단 (BUY + 포지션 0)
                if decision.signal == MACrossSignal.BUY and position_qty == 0:
                    cur_time = df_slice.index[-1].time()
                    if block_start <= cur_time < be:
                        return "hold"

                return decision.signal.value
            return _fn

        cases = [
            ("워밍업 12봉(60분), 진입차단 OFF",  _make_warmup(12, "09:00")),
            ("워밍업 12봉(60분), 차단 09:40",     _make_warmup(12, "09:40")),
            ("워밍업 8봉(40분), 차단 09:40 [추천]",  _make_warmup(8, "09:40")),
            ("워밍업 6봉(30분), 차단 09:40",      _make_warmup(6, "09:40")),
            ("워밍업 4봉(20분), 차단 09:40",      _make_warmup(4, "09:40")),
            ("워밍업 0봉, 차단 09:40",             _make_warmup(0, "09:40")),
        ]
        results = []
        for label, fn in cases:
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<35} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")
        print_table(results)
        return
    elif mode == "entry_block":
        # 시간대 진입 차단 효과 비교 (09:00~09:40 신규 매수 차단)
        from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
        from stock_bot.strategy.ma_cross import MACrossSignal
        from stock_bot.indicators.atr import atr_from_ohlcv
        from stock_bot.backtest.engine import run_strategy
        from datetime import time as _dtime

        print("데이터 다운로드 (5m)...", flush=True)
        df = _download(symbol, period, "5m")

        def _make_block(block_enabled: bool, start_hm: str = "09:00", end_hm: str = "09:40"):
            block_start = _dtime.fromisoformat(start_hm) if block_enabled else None
            block_end = _dtime.fromisoformat(end_hm) if block_enabled else None

            def _fn(df_slice, position_qty, avg_price, stop_loss_pct):
                cfg = EnsembleConfig()
                cfg.vwap_band = 0.0085
                cfg.vwap_warmup_bars = 12
                cfg.rsi_period = 25
                cfg.rsi_oversold = 30.0
                cfg.rsi_overbought = 74.0
                cfg.supertrend_period = 7
                cfg.supertrend_mult = 2.5
                cfg.bb_window = 15
                cfg.bb_k = 1.7
                cfg.bb_consec = 3
                cfg.weights = (0.28, 0.24, 0.16, 0.12, 0.20)
                cfg.volume_filter_enabled = True

                last_price = float(df_slice["close"].iloc[-1])

                # ATR 손절 (×12)
                ohlcv_list = []
                for ts, row in df_slice.iterrows():
                    ohlcv_list.append({"open": row["open"], "high": row["high"],
                                       "low": row["low"], "close": row["close"], "volume": row["volume"]})
                atr_val = atr_from_ohlcv(ohlcv_list, period=14)
                if atr_val > 0:
                    stop_pct = (atr_val * 12.0) / last_price * 100
                else:
                    stop_pct = 5.0

                decision = decide_ensemble(df_slice["close"], df_slice, position_qty, avg_price, stop_pct, cfg)

                # 진입 차단: BUY + 포지션 0 + 차단 시간대
                if block_enabled and decision.signal == MACrossSignal.BUY and position_qty == 0:
                    cur_ts = df_slice.index[-1]
                    cur_time = cur_ts.time()
                    if block_start <= cur_time < block_end:
                        return "hold"

                return decision.signal.value
            return _fn

        cases = [
            ("진입 차단 OFF (현재 ATR×12)",  _make_block(False)),
            ("진입 차단 09:00~09:40",        _make_block(True, "09:00", "09:40")),
            ("진입 차단 09:00~10:00",        _make_block(True, "09:00", "10:00")),
            ("진입 차단 09:00~09:30",        _make_block(True, "09:00", "09:30")),
        ]
        results = []
        for label, fn in cases:
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<32} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}  MDD {r.max_drawdown_pct:>5.1f}%")
        print_table(results)
        return
    elif mode == "current":
        # 현재 라이브 설정 그대로 백테스트 (ATR 손절 + 거래량 필터, Trailing 없음)
        from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
        from stock_bot.indicators.atr import atr_from_ohlcv
        from stock_bot.backtest.engine import run_strategy

        print("데이터 다운로드 (5m)...", flush=True)
        df = _download(symbol, period, "5m")

        def _make_variant(atr_mult: float, use_atr: bool = True):
            def _fn(df_slice, position_qty, avg_price, stop_loss_pct):
                cfg = EnsembleConfig()
                cfg.vwap_band = 0.0085
                cfg.vwap_warmup_bars = 12
                cfg.rsi_period = 25
                cfg.rsi_oversold = 30.0
                cfg.rsi_overbought = 74.0
                cfg.supertrend_period = 7
                cfg.supertrend_mult = 2.5
                cfg.bb_window = 15
                cfg.bb_k = 1.7
                cfg.bb_consec = 3
                cfg.weights = (0.28, 0.24, 0.16, 0.12, 0.20)
                cfg.volume_filter_enabled = True

                last_price = float(df_slice["close"].iloc[-1])

                if use_atr:
                    ohlcv_list = []
                    for ts, row in df_slice.iterrows():
                        ohlcv_list.append({"open": row["open"], "high": row["high"],
                                           "low": row["low"], "close": row["close"], "volume": row["volume"]})
                    atr_val = atr_from_ohlcv(ohlcv_list, period=14)
                    if atr_val > 0:
                        stop_pct = (atr_val * atr_mult) / last_price * 100
                    else:
                        stop_pct = 5.0
                else:
                    stop_pct = 5.0

                decision = decide_ensemble(df_slice["close"], df_slice, position_qty, avg_price, stop_pct, cfg)
                return decision.signal.value
            return _fn

        cases = [
            ("BASE: 고정 5% 손절",      _make_variant(0, use_atr=False)),
            ("ATR ×8 (예전 라이브)",    _make_variant(8.0)),
            ("ATR ×10",                _make_variant(10.0)),
            ("ATR ×12 (추천)",         _make_variant(12.0)),
            ("ATR ×15",                _make_variant(15.0)),
        ]
        results = []
        for label, fn in cases:
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<28} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}  MDD {r.max_drawdown_pct:>5.1f}%")
        print_table(results)
        return
    elif mode == "macd":
        # MACD 단축형(5/13/4)을 6번째 투표자로 추가했을 때 효과
        from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
        from stock_bot.strategy.macd import decide_macd
        from stock_bot.strategy.ma_cross import MACrossSignal
        from stock_bot.backtest.engine import run_strategy

        print("데이터 다운로드 (5m)...", flush=True)
        df = _download(symbol, period, "5m")

        def _make_macd(macd_weight: float):
            """MACD 가중치를 추가, 기존 weights는 비례 축소."""
            def _fn(df, position_qty, avg_price, stop_loss_pct):
                cfg = EnsembleConfig()
                cfg.vwap_band       = 0.0085
                cfg.rsi_oversold    = 30.0
                cfg.rsi_overbought  = 74.0
                cfg.min_sell_votes  = 2
                cfg.supertrend_mult = 2.5
                cfg.supertrend_period = 7
                cfg.rsi_period      = 25
                cfg.bb_window       = 15
                cfg.bb_k            = 1.8
                cfg.bb_consec       = 3
                cfg.weights         = (0.28, 0.24, 0.16, 0.12, 0.20)
                cfg.volume_filter_enabled = True  # 거래량 필터는 켠 채로 비교
                base_decision = decide_ensemble(df["close"], df, position_qty, avg_price, stop_loss_pct, cfg)

                if macd_weight <= 0:
                    return base_decision.signal.value

                # MACD 단축형 (5/13/4) 추가
                m_d = decide_macd(df["close"], 5, 13, 4, position_qty, avg_price, stop_loss_pct=999)
                base_score = base_decision.meta.get("weighted_score", 0.0) if base_decision.meta else 0.0
                macd_score = 1.0 if m_d.signal == MACrossSignal.BUY else (-1.0 if m_d.signal == MACrossSignal.SELL else 0.0)
                final_score = base_score * (1 - macd_weight) + macd_score * macd_weight

                # 매수/매도 결정
                if base_decision.signal.value == "buy":
                    return "buy" if final_score >= cfg.buy_threshold else "hold"
                if base_decision.signal.value == "sell":
                    return "sell" if final_score <= cfg.sell_threshold else "hold"
                # base가 hold인데 MACD가 강하면 신호 발생 가능
                if final_score >= cfg.buy_threshold and position_qty == 0:
                    return "buy"
                if final_score <= cfg.sell_threshold and position_qty > 0:
                    return "sell"
                return "hold"
            return _fn

        cases = [
            ("MACD 미사용 (현재)",     _make_macd(0.0)),
            ("MACD 가중치 0.10",       _make_macd(0.10)),
            ("MACD 가중치 0.15",       _make_macd(0.15)),
            ("MACD 가중치 0.20",       _make_macd(0.20)),
        ]
        results = []
        for label, fn in cases:
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<28} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")
        print_table(results)
        return
    elif mode == "adx":
        # ADX 기반 동적 가중치: 추세장/횡보장 따라 가중치 변경
        from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
        from stock_bot.indicators.adx import adx as compute_adx
        from stock_bot.backtest.engine import run_strategy

        print("데이터 다운로드 (5m)...", flush=True)
        df = _download(symbol, period, "5m")

        # ADX 미리 계산 (전체 시계열)
        adx_series = compute_adx(df["high"], df["low"], df["close"], period=14)

        def _make_adx(use_dynamic: bool):
            def _fn(df_slice, position_qty, avg_price, stop_loss_pct):
                cfg = EnsembleConfig()
                cfg.vwap_band       = 0.0085
                cfg.rsi_oversold    = 30.0
                cfg.rsi_overbought  = 74.0
                cfg.min_sell_votes  = 2
                cfg.supertrend_mult = 2.5
                cfg.supertrend_period = 7
                cfg.rsi_period      = 25
                cfg.bb_window       = 15
                cfg.bb_k            = 1.8
                cfg.bb_consec       = 3
                cfg.volume_filter_enabled = True

                if use_dynamic:
                    # 현재 ADX 값 조회
                    cur_idx = df_slice.index[-1]
                    adx_val = adx_series.get(cur_idx, float("nan"))
                    if pd.notna(adx_val):
                        if adx_val > 25:
                            # 추세장: VWAP/Supertrend 강화
                            cfg.weights = (0.35, 0.30, 0.10, 0.05, 0.20)
                        elif adx_val < 20:
                            # 횡보장: BB/RSI 강화
                            cfg.weights = (0.20, 0.15, 0.20, 0.25, 0.20)
                        else:
                            cfg.weights = (0.28, 0.24, 0.16, 0.12, 0.20)
                    else:
                        cfg.weights = (0.28, 0.24, 0.16, 0.12, 0.20)
                else:
                    cfg.weights = (0.28, 0.24, 0.16, 0.12, 0.20)

                sig = decide_ensemble(df_slice["close"], df_slice, position_qty, avg_price, stop_loss_pct, cfg)
                return sig.signal.value
            return _fn

        cases = [
            ("ADX 미사용 (고정 가중치)", _make_adx(False)),
            ("ADX 동적 가중치",          _make_adx(True)),
        ]
        results = []
        for label, fn in cases:
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<28} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")
        print_table(results)
        return
    elif mode == "vol_modes":
        # A/B/C 안 비교: 점수만 vs 점수+veto vs 투표만 vs 투표+점수
        from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble

        print("데이터 다운로드 (5m)...", flush=True)
        df = _download(symbol, period, "5m")

        def _make_mode(score_mod: bool, buy_veto: bool, voter: bool):
            def _fn(df, position_qty, avg_price, stop_loss_pct):
                cfg = EnsembleConfig()
                cfg.vwap_band       = 0.0085
                cfg.rsi_oversold    = 30.0
                cfg.rsi_overbought  = 74.0
                cfg.min_sell_votes  = 2
                cfg.supertrend_mult = 2.5
                cfg.supertrend_period = 7
                cfg.rsi_period      = 25
                cfg.bb_window       = 15
                cfg.bb_k            = 1.8
                cfg.bb_consec       = 3
                cfg.weights         = (0.28, 0.24, 0.16, 0.12, 0.20)
                cfg.volume_filter_enabled    = score_mod
                cfg.volume_buy_veto_enabled  = buy_veto
                cfg.volume_as_voter_enabled  = voter
                cfg.volume_high_ratio        = 1.2
                cfg.volume_low_ratio         = 0.7
                cfg.volume_buy_veto_ratio    = 1.0
                cfg.volume_score_boost       = 0.10
                cfg.volume_score_penalty     = 0.05
                cfg.volume_voter_weight      = 0.10
                sig = decide_ensemble(df["close"], df, position_qty, avg_price, stop_loss_pct, cfg)
                return sig.signal.value
            return _fn

        cases = [
            ("기본 (필터 OFF)",                    _make_mode(False, False, False)),
            ("[원래] 점수만",                       _make_mode(True,  False, False)),
            ("[A안] 점수 + 매수거부권",              _make_mode(True,  True,  False)),
            ("[B안] 거래량 투표만",                 _make_mode(False, False, True)),
            ("[C안] 점수 + 거래량 투표",             _make_mode(True,  False, True)),
        ]
        results = []
        for label, fn in cases:
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<32} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")
        print_table(results)
        return
    elif mode == "volume":
        # 거래량 확인 필터 비교: 미적용 vs 적용 (다양한 임계값)
        from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble

        print("데이터 다운로드 (5m)...", flush=True)
        df = _download(symbol, period, "5m")

        def _make_vol(enabled: bool, high_ratio: float = 1.2, low_ratio: float = 0.7,
                      boost: float = 0.10, penalty: float = 0.05):
            def _fn(df, position_qty, avg_price, stop_loss_pct):
                cfg = EnsembleConfig()
                cfg.vwap_band       = 0.0085
                cfg.rsi_oversold    = 30.0
                cfg.rsi_overbought  = 74.0
                cfg.min_sell_votes  = 2
                cfg.supertrend_mult = 2.5
                cfg.supertrend_period = 7
                cfg.rsi_period      = 25
                cfg.bb_window       = 15
                cfg.bb_k            = 1.8
                cfg.bb_consec       = 3
                cfg.weights         = (0.28, 0.24, 0.16, 0.12, 0.20)
                cfg.volume_filter_enabled = enabled
                cfg.volume_high_ratio = high_ratio
                cfg.volume_low_ratio  = low_ratio
                cfg.volume_score_boost = boost
                cfg.volume_score_penalty = penalty
                sig = decide_ensemble(df["close"], df, position_qty, avg_price, stop_loss_pct, cfg)
                return sig.signal.value
            return _fn

        cases = [
            ("거래량 필터 OFF (현재)",          _make_vol(False)),
            ("거래량 필터 ON (1.2/0.7, 0.10)",   _make_vol(True, 1.2, 0.7, 0.10, 0.05)),
            ("거래량 필터 강화 (1.5/0.5, 0.15)", _make_vol(True, 1.5, 0.5, 0.15, 0.10)),
            ("거래량 필터 약 (1.1/0.8, 0.05)",  _make_vol(True, 1.1, 0.8, 0.05, 0.03)),
        ]
        results = []
        for label, fn in cases:
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<32} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")
        print_table(results)
        return
    elif mode == "bb_consec":
        # BB 꺾임 감지 연속봉 수 비교: 2봉 vs 3봉
        from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble

        print("데이터 다운로드 (5m)...", flush=True)
        df = _download(symbol, period, "5m")

        def _make_consec(consec: int):
            def _fn(df, position_qty, avg_price, stop_loss_pct):
                cfg = EnsembleConfig()
                cfg.vwap_band       = 0.0085
                cfg.rsi_oversold    = 30.0
                cfg.rsi_overbought  = 74.0
                cfg.min_sell_votes  = 2
                cfg.supertrend_mult = 2.5
                cfg.supertrend_period = 7
                cfg.rsi_period      = 25
                cfg.bb_window       = 15
                cfg.bb_k            = 1.8
                cfg.bb_consec       = consec
                cfg.weights         = (0.28, 0.24, 0.16, 0.12, 0.20)
                sig = decide_ensemble(df["close"], df, position_qty, avg_price, stop_loss_pct, cfg)
                return sig.signal.value
            return _fn

        cases = [
            ("BB 꺾임 3봉 연속 (현재)", 3),
            ("BB 꺾임 2봉 연속",        2),
        ]
        results = []
        for label, consec in cases:
            r = run_strategy(df, _make_consec(consec), label)
            results.append((label, r))
            print(f"  {label:<28} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")
        print_table(results)
        return
    elif mode == "st_period":
        # Supertrend period 스윕 (현재 RSI=21, BB=15/1.8 반영)
        # label, interval, vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k, st_period, rsi_period
        cases = [
            ("ST period=3  (매우 빠름)", "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 3,  21),
            ("ST period=5  (빠름)",      "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 5,  21),
            ("ST period=7  (현재)",      "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 7,  21),
            ("ST period=9  (느림)",      "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 9,  21),
            ("ST period=10 (매우 느림)", "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.8, 10, 21),
        ]
        results = []
        df_cache: dict[str, pd.DataFrame] = {}
        for label, interval, vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k, st_p, rsi_p in cases:
            if interval not in df_cache:
                print(f"데이터 다운로드 ({interval})...", flush=True)
                df_cache[interval] = _download(symbol, period, interval)
            df = df_cache[interval]
            fn = _make(vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k, st_period=st_p, rsi_period=rsi_p)
            r = run_strategy(df, fn, label)
            results.append((label, r))
            print(f"  {label:<28} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")
        print_table(results)
        return
    elif mode == "vwap":
        bands = [0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.010, 0.012, 0.015]
        cases = [(f"vwap={b:.3f}", "5m", b, 30.0, 72.0, 2, 3.0, 2.0) for b in bands]
    elif mode == "supertrend":
        cases = [
            ("현재: ST mult=3.0, BB k=2.0", "5m", 0.007, 30.0, 72.0, 2, 3.0, 2.0),
            ("ST mult=2.0, BB k=2.0",        "5m", 0.007, 30.0, 72.0, 2, 2.0, 2.0),
            ("ST mult=2.0, BB k=1.5",        "5m", 0.007, 30.0, 72.0, 2, 2.0, 1.5),
            ("ST mult=3.0, BB k=1.5",        "5m", 0.007, 30.0, 72.0, 2, 3.0, 1.5),
        ]
    elif mode == "fraction":
        # 포지션 비율 스윕 — engine은 항상 95% cash 쓰므로 수익률 변화 없음
        # 대신 실제 금액 기준 수익 추정
        account = 10_000_000
        price   = 58_000
        fracs   = [0.20, 0.30, 0.40, 0.50, 0.60]
        print(f"\n계좌 {account:,}원 / 삼성 {price:,}원 기준 매수금액·주수 시뮬레이션")
        print(f"{'비율':>6}  {'매수금액':>12}  {'주수':>6}  {'1%이익':>10}  {'5%이익':>10}  {'MDD시손실':>12}")
        print("-" * 65)
        for f in fracs:
            budget = account * f
            qty    = int(budget // price)
            gain1  = qty * price * 0.01
            gain5  = qty * price * 0.05
            loss_mdd = qty * price * 0.083  # 백테스트 MDD 8.3% 기준
            print(f"{f*100:>5.0f}%  {budget:>12,.0f}원  {qty:>5}주  {gain1:>9,.0f}원  {gain5:>9,.0f}원  -{loss_mdd:>9,.0f}원")
        print()
        return
    else:
        cases = [
            ("A: 현재(vwap=0.007,rsi=30/72,sell=2,5m)", "5m",  0.007, 30.0, 72.0, 2, 3.0, 2.0),
            ("B: A + min_sell=3 (5m)",                   "5m",  0.007, 30.0, 72.0, 3, 3.0, 2.0),
            ("C: B + 10분봉 (min_sell=3, 10m)",           "10m", 0.007, 30.0, 72.0, 3, 3.0, 2.0),
        ]

    results = []
    df_cache: dict[str, pd.DataFrame] = {}

    for label, interval, vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k in cases:
        if interval not in df_cache:
            print(f"데이터 다운로드 ({interval})...", flush=True)
            df_cache[interval] = _download(symbol, period, interval)
        df = df_cache[interval]

        fn = _make(vwap, rsi_os, rsi_ob, min_sell, st_mult, bb_k, rsi_period=21)
        r = run_strategy(df, fn, label)
        results.append((label, r))
        print(f"  {label:<28} 수익 {r.total_return_pct:>+7.2f}%  거래 {r.trades:>3}회  승률 {r.win_rate:>5.1f}%  샤프 {r.sharpe:>6.2f}")

    print_table(results)


if __name__ == "__main__":
    main()
