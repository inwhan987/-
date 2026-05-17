"""필터 비교 백테스트 — 실전 러너 완전 동일 설정.

backtest_current.py 와 100% 동일한 .env.overrides 설정을 사용하며,
HTF 차단 필터만 바꿔 가며 수익률·승률을 비교합니다.

사용:
  python backtest_compare.py [symbol1,symbol2,...] [period]
  예) python backtest_compare.py 005930.KS 60d
  예) python backtest_compare.py 005930.KS,035720.KS,000660.KS 60d
"""
from __future__ import annotations

import sys

import pandas as pd

# backtest_current.py 의 핵심 함수 재사용 (if __name__ 가드 덕분에 import 가능)
from backtest_current import (
    _load_env,
    _download,
    _make_current,
    _wrap_htf,
    ATR_STOP_MAX_PCT,
)
from stock_bot.backtest.engine import run_strategy


def _make_adx_dir(df5m: pd.DataFrame, tf_min: int, adx_period: int,
                  adx_threshold: float, di_spread_min: float = 0.0) -> pd.Series:
    """5분봉 → HTF ADX 방향 (1=상승, -1=하락). lookahead 없음.
    차단 조건: ADX > adx_threshold AND -DI > +DI AND (-DI - +DI) >= di_spread_min
    """
    htf = df5m.resample(f"{tf_min}min", label="left", closed="left").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])

    hi, lo, cl = htf["high"], htf["low"], htf["close"]
    tr    = pd.concat([hi - lo, (hi - cl.shift(1)).abs(), (lo - cl.shift(1)).abs()], axis=1).max(axis=1)
    dm_pr = (hi - hi.shift(1)).clip(lower=0)
    dm_mr = (lo.shift(1) - lo).clip(lower=0)
    dm_p  = dm_pr.where(dm_pr > dm_mr, 0.0)
    dm_m  = dm_mr.where(dm_mr > dm_pr, 0.0)

    def wilder(s, n):
        r = s.copy().astype(float) * float("nan")
        r.iloc[n] = s.iloc[1:n+1].sum()
        for i in range(n + 1, len(s)):
            r.iloc[i] = r.iloc[i-1] - r.iloc[i-1] / n + s.iloc[i]
        return r

    n     = adx_period
    atr_s = wilder(tr, n)
    dip_s = wilder(dm_p, n)
    dim_s = wilder(dm_m, n)
    di_p  = (dip_s / atr_s * 100).replace([float("inf"), float("-inf")], float("nan"))
    di_m  = (dim_s / atr_s * 100).replace([float("inf"), float("-inf")], float("nan"))
    dx    = ((di_p - di_m).abs() / (di_p + di_m) * 100).replace([float("inf"), float("-inf")], float("nan"))
    adx   = wilder(dx, n)

    # lookahead 방지: shift(2)
    adx_s  = adx.shift(2)
    di_p_s = di_p.shift(2)
    di_m_s = di_m.shift(2)
    spread = di_m_s - di_p_s  # 양수 = -DI가 크다 = 하락 압력

    d = pd.Series(1, index=htf.index)
    block = (adx_s > adx_threshold) & (di_m_s > di_p_s) & (spread >= di_spread_min)
    d[block] = -1
    return d.reindex(df5m.index, method="ffill").fillna(1)


def _run_one(df: pd.DataFrame, fn, symbol: str, env: dict):
    """run_strategy 를 실전 러너와 100% 동일한 파라미터로 실행."""
    return run_strategy(
        df, fn, symbol,
        stop_loss_pct          = ATR_STOP_MAX_PCT,
        enable_add_buy         = env.get("ADD_BUY_ENABLED",              "false").lower() == "true",
        add_buy_fraction       = float(env.get("ADD_BUY_FRACTION",        "0.20")),
        add_buy_max_count      = int(  env.get("ADD_BUY_MAX_COUNT",       "2")),
        add_buy_max_position_pct = float(env.get("ADD_BUY_MAX_POSITION_PCT", "0.80")),
        inherit_initial_stop   = env.get("ADD_BUY_INHERIT_INITIAL_STOP",  "true").lower() == "true",
        post_stoploss_cooldown_min = int(env.get("POST_STOPLOSS_COOLDOWN_MIN", "30")),
        initial_position_fraction  = float(env.get("POSITION_FRACTION",   "0.40")),
        bar_minutes            = 5,
        sell_on_next_open      = env.get("SELL_ON_NEXT_OPEN",    "true").lower() == "true",
    )


def main():
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    symbols     = [s.strip() for s in symbols_str.split(",")]

    env = _load_env()

    # ── 출력용 설정 요약 ──────────────────────────────────────────────────
    vb        = float(env.get("TRADE_VWAP_BAND",         "0.008")) * 100
    vsb       = float(env.get("TRADE_VWAP_SELL_BAND",    "0.0085")) * 100
    rp        = env.get("TRADE_RSI_PERIOD",          "25")
    sp        = env.get("TRADE_SUPERTREND_PERIOD",   "7")
    sm        = env.get("TRADE_SUPERTREND_MULT",     "2.5")
    pos_frac  = float(env.get("POSITION_FRACTION",   "0.40"))
    cooldown  = int(  env.get("POST_STOPLOSS_COOLDOWN_MIN", "30"))
    add_buy   = env.get("ADD_BUY_ENABLED", "false").lower() == "true"
    sell_next = env.get("SELL_ON_NEXT_OPEN", "true").lower() == "true"

    # ── HTF 기본값 (현재 .env.overrides) ──────────────────────────────────
    htf_tf      = int(  env.get("HTF_BLOCK_TF_MINUTES",    "30"))
    htf_adx_p   = int(  env.get("HTF_BLOCK_ADX_PERIOD",    "14"))
    htf_adx_thr = float(env.get("HTF_BLOCK_ADX_THRESHOLD", "30.0"))
    htf_ov_en   = env.get("HTF_MA_OVERRIDE_ENABLED", "true").lower() == "true"
    htf_ov_span = int(  env.get("HTF_MA_OVERRIDE_SPAN",    "120"))
    htf_ov_pct  = float(env.get("HTF_MA_OVERRIDE_PCT",     "1.5"))

    print(f"\n기간: {period}  전략: 현재 앙상블 (VWAP{vb:.2f}%/{vsb:.2f}%/RSI{rp}/ST{sp}×{sm}/ATR캡5%)")
    print(f"설정: 초기진입={pos_frac*100:.0f}%  쿨다운={cooldown}분  추가매수={add_buy}  시가매도={sell_next}")
    print(f"종목: {', '.join(symbols)}\n")

    # ── 비교할 필터 목록: (레이블, htf_enabled, tf_min, adx_period, adx_threshold, di_spread_min) ──
    # di_spread_min: -DI - +DI 최소 차이 (추가 조건, 0이면 미사용)
    # 현재 .env.overrides 설정은 ★ 로 표시
    filters = [
        ("차단없음",              False, 30, 14,  0.0, 0),
        (f"ADX>30 p=14 30분 ★",  True,  30, 14, 30.0, 0),
        ("ADX>35 p=14 30분",     True,  30, 14, 35.0, 0),
        ("ADX>40 p=14 30분",     True,  30, 14, 40.0, 0),
        ("ADX>30+DI차5 30분",    True,  30, 14, 30.0, 5),
        ("ADX>30+DI차10 30분",   True,  30, 14, 30.0, 10),
        ("ADX>35+DI차5 30분",    True,  30, 14, 35.0, 5),
        ("ADX>30 p=14 60분",     True,  60, 14, 30.0, 0),
        ("ADX>35 p=14 60분",     True,  60, 14, 35.0, 0),
    ]

    # ── 데이터 다운로드 (종목당 1회) ──────────────────────────────────────
    dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        print(f"  {sym} 다운로드 중...", end=" ", flush=True)
        try:
            dfs[sym] = _download(sym, period)
            print(f"{len(dfs[sym])}봉")
        except Exception as e:
            print(f"실패: {e}")

    base_fn = _make_current()

    # ── 테이블 헤더 ───────────────────────────────────────────────────────
    label_w = max(len(f[0]) for f in filters) + 2
    col_w   = max(10, max(len(s.replace(".KS", "")) + 2 for s in symbols))
    multi   = len(symbols) > 1

    hdr_syms = "".join(f"{s.replace('.KS',''):>{col_w}}" for s in symbols)
    avg_hdr  = f"{'평균':>{col_w}}{'평균승률':>{col_w}}" if multi else ""
    sep_len  = label_w + col_w * len(symbols) + (col_w * 2 if multi else 0)
    sep      = "=" * sep_len

    print(sep)
    print(f"{'필터':<{label_w}}{hdr_syms}{avg_hdr}")
    print("-" * sep_len)

    # ── 필터별 백테스트 실행 ──────────────────────────────────────────────
    summary = []  # (label, avg_ret)

    for label, htf_en, tf_min, adx_p, adx_thr, di_spread in filters:
        rets, wrs = [], []

        for sym in symbols:
            if sym not in dfs:
                rets.append(None); wrs.append(None)
                continue
            try:
                if htf_en:
                    htf_dir = _make_adx_dir(dfs[sym], tf_min, adx_p, adx_thr, di_spread)
                    fn = _wrap_htf(base_fn, htf_dir, htf_ov_en, htf_ov_span, htf_ov_pct)
                else:
                    fn = base_fn
                r = _run_one(dfs[sym], fn, sym, env)
                rets.append(r.total_return_pct)
                wrs.append(r.win_rate)
            except Exception as e:
                print(f"  [{label}] {sym} 오류: {e}")
                rets.append(None); wrs.append(None)

        ret_cells = "".join(
            f"{v:>+{col_w}.2f}%" if v is not None else f"{'오류':>{col_w}}"
            for v in rets
        )
        valid_r = [v for v in rets if v is not None]
        valid_w = [v for v in wrs  if v is not None]
        avg_r   = sum(valid_r) / len(valid_r) if valid_r else None
        avg_w   = sum(valid_w) / len(valid_w) if valid_w else None

        avg_cells = (
            f"{avg_r:>+{col_w}.2f}%{avg_w:>{col_w}.1f}%"
            if avg_r is not None and multi else ""
        )
        print(f"{label:<{label_w}}{ret_cells}{avg_cells}")
        summary.append((label, avg_r))

    print(sep)

    # ── 결론 ──────────────────────────────────────────────────────────────
    if len(summary) >= 2 and all(s[1] is not None for s in summary):
        base_ret = summary[0][1]   # 차단없음
        best     = max(summary, key=lambda x: x[1])
        diff     = best[1] - base_ret
        print(f"\n★ 최고: {best[0].strip()}  평균 {best[1]:+.2f}%  (차단없음 대비 {diff:+.2f}%p)")


if __name__ == "__main__":
    main()
