"""HTF 하락추세 차단 + MA 근접 시 차단 해제 백테스트.

흐름:
  1. 30분봉 EMA20 → 하락추세 → 신규 매수 차단
  2. 단, 현재가가 5분봉 MA(단기 이평선) 부근이면 차단 해제 → 반등 진입 허용

비교 설정:
  ① 기준선 0.45                  (HTF 블록 없음)
  ② HTF EMA20 완전차단           (이전 구현)
  ③ HTF + MA20_5m 1% 오버라이드  (신규)
  ④ HTF + MA20_5m 1.5% 오버라이드
  ⑤ HTF + MA60_5m 1% 오버라이드
  ⑥ HTF + MA60_5m 1.5% 오버라이드
  ⑦ HTF + MA120_5m 1% 오버라이드
  ⑧ HTF + MA120_5m 1.5% 오버라이드
"""
from __future__ import annotations
import os, tempfile, shutil, warnings
warnings.filterwarnings("ignore")

try:
    import certifi
    _d = os.path.join(tempfile.gettempdir(), "cacert.pem")
    if not os.path.exists(_d): shutil.copy(certifi.where(), _d)
    os.environ.setdefault("CURL_CA_BUNDLE", _d)
    os.environ.setdefault("SSL_CERT_FILE", _d)
except Exception: pass

import pandas as pd
import numpy as np
import yfinance as yf

from stock_bot.backtest.engine import run_strategy
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble
from stock_bot.indicators.atr import atr_from_ohlcv


# ── HTF EMA20 추세 판단 ────────────────────────────────────────────────────────

def _resample_30m(df5m: pd.DataFrame) -> pd.Series:
    """5분봉 → 30분봉 EMA20. 완성봉 기준(shift), lookahead 없음."""
    htf = df5m.resample("30min", label="left", closed="left").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])
    ema = htf["close"].ewm(span=20, adjust=False).mean().shift(1)
    direction = pd.Series(1, index=htf.index)
    direction[htf["close"] < ema] = -1
    return direction.shift(1).reindex(df5m.index, method="ffill").fillna(1)


# ── 공통 앙상블 결정 ───────────────────────────────────────────────────────────

def _base_decide(df_slice, pos, avg):
    cfg = EnsembleConfig()
    cfg.vwap_band=0.008; cfg.vwap_sell_band=0.0085; cfg.vwap_st_bull_sell_band=0.009
    cfg.vwap_warmup_bars=8; cfg.rsi_period=25; cfg.rsi_oversold=30.0; cfg.rsi_overbought=74.0
    cfg.supertrend_period=7; cfg.supertrend_mult=2.5
    cfg.bb_window=20; cfg.bb_k=2.0; cfg.bb_consec=3
    cfg.weights=(0.25, 0.22, 0.20, 0.18, 0.15)
    cfg.min_buy_votes=2; cfg.buy_threshold=0.45
    cfg.min_sell_votes=2; cfg.sell_threshold=-0.40
    cfg.volume_filter_enabled=True; cfg.volume_high_ratio=1.2; cfg.volume_low_ratio=0.7
    cfg.volume_score_boost=0.10; cfg.volume_score_penalty=0.05
    lp = float(df_slice["close"].iloc[-1])
    ol = [{"open":r.open,"high":r.high,"low":r.low,"close":r.close,"volume":r.volume}
          for r in df_slice.itertuples()]
    av = atr_from_ohlcv(ol, period=14)
    sp = min((av * 12.0) / lp * 100, 5.0) if av > 0 and lp > 0 else 5.0
    td = df_slice.index[-1].date()
    dt = df_slice[df_slice.index.date == td]
    d = decide_ensemble(df_slice["close"], ohlcv_df=dt, ohlcv_df_hist=df_slice,
                        position_qty=pos, avg_price=avg, stop_loss_pct=sp, config=cfg)
    return d.signal.value


# ── 전략 팩토리 ────────────────────────────────────────────────────────────────

def make_baseline():
    def fn(df_slice, pos, avg, sl, ctx=None):
        return _base_decide(df_slice, pos, avg)
    return fn


def make_htf_block(htf_dir: pd.Series):
    """30분봉 EMA20 하락추세 시 신규 매수 완전 차단."""
    def fn(df_slice, pos, avg, sl, ctx=None):
        now = df_slice.index[-1]
        try: d = float(htf_dir.loc[now])
        except: d = 1.0
        sig = _base_decide(df_slice, pos, avg)
        if d <= 0 and sig == "buy" and pos == 0:
            return "hold"
        return sig
    return fn


def make_htf_block_ma_override(htf_dir: pd.Series, ma_period: int, proximity_pct: float):
    """30분봉 EMA20 하락추세 차단 + 5분봉 MA 부근이면 차단 해제.

    Args:
        ma_period: 5분봉 단순/지수 이동평균 기간 (EMA 사용)
        proximity_pct: MA와의 거리 비율 임계값 (예: 1.0 → 1% 이내면 오버라이드)
    """
    def fn(df_slice, pos, avg, sl, ctx=None):
        now = df_slice.index[-1]
        try: d = float(htf_dir.loc[now])
        except: d = 1.0
        sig = _base_decide(df_slice, pos, avg)

        # 하락추세 + 포지션 없을 때 buy
        if d <= 0 and sig == "buy" and pos == 0:
            # MA 근접 여부 체크
            if len(df_slice) >= ma_period:
                ma_val = float(df_slice["close"].ewm(span=ma_period, adjust=False).mean().iloc[-1])
                cur_price = float(df_slice["close"].iloc[-1])
                dist_pct = abs(cur_price - ma_val) / ma_val * 100
                if dist_pct <= proximity_pct:
                    # MA 부근 → 차단 해제, 반등 진입 허용
                    return "buy"
            # MA 부근 아님 → 차단
            return "hold"
        return sig
    return fn


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def _download(sym: str) -> pd.DataFrame:
    df = yf.download(sym, period="60d", interval="5m", auto_adjust=True, progress=False)
    if df.empty: raise ValueError(f"없음: {sym}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    return df

def _run(fn, df):
    return run_strategy(df, fn, "test", stop_loss_pct=5.0, enable_add_buy=False,
                        initial_position_fraction=0.70, bar_minutes=5, sell_on_next_open=True)

def _bh(df):
    c = df["close"]
    return (float(c.iloc[-1]) - float(c.iloc[0])) / float(c.iloc[0]) * 100


TARGETS = [
    ("005930.KS", "삼성전자", "상승"),
    ("000660.KS", "SK하이닉스", "급등"),
    ("035420.KS", "NAVER",    "하락"),
    ("035720.KS", "카카오",   "하락"),
    ("068270.KS", "셀트리온", "하락"),
    ("105560.KS", "KB금융",   "보합"),
]


def main():
    print("다운로드 중...", end=" ", flush=True)
    data = {}
    for sym, name, trend in TARGETS:
        try:
            data[sym] = (_download(sym), name, trend)
            print(name, end=" ", flush=True)
        except Exception as e:
            print(f"\n{name} 실패: {e}")
    print(f"\n{len(data)}종목 완료\n")

    bear_syms = [s for s, (_, _, t) in data.items() if t == "하락"]
    bull_syms = [s for s, (_, _, t) in data.items() if t in ("상승", "급등")]

    # HTF 추세 계산
    print("HTF EMA20 계산 중...", flush=True)
    htf_dirs = {sym: _resample_30m(df5m) for sym, (df5m, _, _) in data.items()}

    # MA 오버라이드 발동 빈도 통계 출력
    print("\n[MA 근접 오버라이드 발동 예상 빈도 (하락추세 + BUY 신호 중 MA X% 이내 비율)]")
    for sym, (df5m, name, _) in data.items():
        htf = htf_dirs[sym]
        down_mask = htf <= 0
        row = f"  {name:<10}"
        for period in [20, 60, 120]:
            ma = df5m["close"].ewm(span=period, adjust=False).mean()
            dist = (df5m["close"] - ma).abs() / ma * 100
            n_down = down_mask.sum()
            if n_down > 0:
                within_1 = (down_mask & (dist <= 1.0)).sum()
                within_15 = (down_mask & (dist <= 1.5)).sum()
                row += f"  EMA{period}: 1%={within_1/n_down*100:.0f}% 1.5%={within_15/n_down*100:.0f}%"
        print(row)

    COMBOS = [
        ("① 기준선 0.45",           make_baseline, None, None, None),
        ("② HTF 완전차단",           make_htf_block, None, None, None),
        ("③ HTF+EMA20_5m 1%",       make_htf_block_ma_override, 20, 1.0, None),
        ("④ HTF+EMA20_5m 1.5%",     make_htf_block_ma_override, 20, 1.5, None),
        ("⑤ HTF+EMA60_5m 1%",       make_htf_block_ma_override, 60, 1.0, None),
        ("⑥ HTF+EMA60_5m 1.5%",     make_htf_block_ma_override, 60, 1.5, None),
        ("⑦ HTF+EMA120_5m 1%",      make_htf_block_ma_override, 120, 1.0, None),
        ("⑧ HTF+EMA120_5m 1.5%",    make_htf_block_ma_override, 120, 1.5, None),
    ]

    print(f"\n{'설정':<26}", end="")
    for sym, (_, name, trend) in data.items():
        bh = _bh(data[sym][0])
        print(f"  {name[:4]}({trend[:2]},BH{bh:>+.0f}%)", end="")
    print(f"  {'하락평균':>8}  {'상승평균':>8}  {'전체평균':>8}")
    print("=" * 125)

    results = []
    for row in COMBOS:
        label, factory = row[0], row[1]
        rets = {}
        for sym, (df5m, name, trend) in data.items():
            try:
                htf = htf_dirs[sym]
                if factory is make_baseline:
                    fn = factory()
                elif factory is make_htf_block:
                    fn = factory(htf)
                else:
                    _, _, period, prox, _ = row
                    fn = factory(htf, period, prox)
                r = _run(fn, df5m)
                rets[sym] = (r.total_return_pct, r.trades, r.win_rate, r.max_drawdown_pct, trend)
            except Exception as e:
                rets[sym] = (0, 0, 0, 0, "오류")
                print(f"  {name} 오류: {e}")

        bear_avg = sum(rets[s][0] for s in bear_syms) / len(bear_syms)
        bull_avg = sum(rets[s][0] for s in bull_syms) / len(bull_syms)
        all_avg  = sum(v[0] for v in rets.values()) / len(rets)

        print(f"{label:<26}", end="")
        for sym in data:
            ret, tr, wr, mdd, _ = rets[sym]
            mk = "▼" if ret < 0 else " "
            print(f"  {ret:>+6.1f}%(tr{tr:>2}){mk}", end="")
        print(f"  {bear_avg:>+8.2f}%  {bull_avg:>+8.2f}%  {all_avg:>+8.2f}%")
        results.append((label, rets, bear_avg, bull_avg, all_avg))

    print("=" * 125)
    base = results[0]
    print(f"\n[기준선(0.45) 대비 개선 - 전체평균 순]")
    print(f"{'설정':<26}  {'하락평균':>8}  {'하락개선':>8}  {'상승평균':>8}  {'상승변화':>8}  {'전체평균':>8}  {'전체개선':>8}")
    print("-" * 94)
    for label, rets, bear_avg, bull_avg, all_avg in sorted(results[1:], key=lambda x: x[4] - base[4], reverse=True):
        print(f"{label:<26}  {bear_avg:>+8.2f}%  {bear_avg-base[2]:>+8.2f}%  "
              f"{bull_avg:>+8.2f}%  {bull_avg-base[3]:>+8.2f}%  "
              f"{all_avg:>+8.2f}%  {all_avg-base[4]:>+8.2f}%")


if __name__ == "__main__":
    main()
