"""스크리너 기술적 점수 상위 N개 종목을 골라 백테스트.

사용:
  python backtest_screener_top10.py [period] [top_n]
  예) python backtest_screener_top10.py 60d 10
"""
from __future__ import annotations

import os, sys, tempfile, shutil, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# SSL 우회
try:
    import certifi
    _dst = os.path.join(tempfile.gettempdir(), "cacert.pem")
    if not os.path.exists(_dst):
        shutil.copy(certifi.where(), _dst)
    os.environ.setdefault("CURL_CA_BUNDLE", _dst)
    os.environ.setdefault("SSL_CERT_FILE", _dst)
except Exception:
    pass

import pandas as pd

HERE = Path(__file__).parent
os.environ.setdefault("DART_API_KEY", "dummy")

# screener 모듈 로드
import importlib.util
_spec = importlib.util.spec_from_file_location("screener", HERE / "screener.py")
_scr  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_scr)

from backtest_current import _load_env, _download, _make_current, ATR_STOP_MAX_PCT, _make_htf_dir, _wrap_htf
from stock_bot.backtest.engine import run_strategy
from stock_bot.indicators.atr import atr_from_ohlcv


def _parse_candidates() -> list[str]:
    cands = []
    p = HERE / "screener_candidates.txt"
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            sym = line.split()[0]
            cands.append(sym)
    return cands


def _score_one(sym: str) -> tuple[str, float, dict]:
    score, detail = _scr.tech_score(sym)
    return sym, score, detail


def _sym_to_name(sym: str) -> str:
    names = {
        "005930.KS": "삼성전자",    "000660.KS": "SK하이닉스",
        "006400.KS": "삼성SDI",     "009150.KS": "삼성전기",
        "066570.KS": "LG전자",      "035720.KS": "카카오",
        "035420.KS": "NAVER",       "068270.KS": "셀트리온",
        "207940.KS": "삼성바이오",  "128940.KS": "한미약품",
        "000100.KS": "유한양행",    "185750.KS": "종근당",
        "005380.KS": "현대차",      "000270.KS": "기아",
        "051910.KS": "LG화학",      "373220.KS": "LG에너지솔",
        "247540.KS": "에코프로비엠","086520.KS": "에코프로",
        "105560.KS": "KB금융",      "055550.KS": "신한지주",
        "086790.KS": "하나금융",    "316140.KS": "우리금융",
        "030200.KS": "KT",          "017670.KS": "SK텔레콤",
        "015760.KS": "한국전력",    "005490.KS": "POSCO홀딩스",
        "011070.KS": "LG이노텍",    "010950.KS": "S-Oil",
        "000720.KS": "현대건설",    "009540.KS": "HD한국조선",
        "329180.KS": "HD현대중공",
    }
    return names.get(sym, sym.split(".")[0])


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else "60d"
    top_n  = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    candidates = _parse_candidates()
    print(f"\n[스크리너] 후보 {len(candidates)}개 기술적 점수 계산 중...")

    scored: list[tuple[str, float, dict]] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_score_one, sym): sym for sym in candidates}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, score, detail = fut.result()
            name = _sym_to_name(sym)
            print(f"  [{i:2d}/{len(candidates)}] {name:<12} {score:>+6.1f}  "
                  f"EMA6m:{detail.get('월봉EMA6','?')[:4]}  "
                  f"RS:{detail.get('RS_KOSPI','?')[:8]}  "
                  f"ADX:{detail.get('ADX','?')[:10]}")
            scored.append((sym, score, detail))

    scored.sort(key=lambda x: -x[1])

    print(f"\n[스크리너 상위 {top_n}개]")
    print("-" * 55)
    for rank, (sym, score, _) in enumerate(scored[:top_n], 1):
        print(f"  {rank:2d}위  {_sym_to_name(sym):<12} ({sym})  점수:{score:>+6.1f}")
    print()

    # 음수 종목 경고
    neg = [(sym, score) for sym, score, _ in scored if score < 0]
    if neg:
        print(f"  ※ 점수 음수 제외 후보: {', '.join(_sym_to_name(s) for s,_ in neg)}")
        print()

    top_symbols = [sym for sym, _, _ in scored[:top_n]]

    # ── 백테스트 ──
    env = _load_env()
    weights        = env.get("ENSEMBLE_WEIGHTS", "0.225,0.225,0.225,0.225,0.10")
    buy_thr        = float(env.get("ENSEMBLE_BUY_THRESHOLD",  "0.50"))
    sell_thr       = float(env.get("ENSEMBLE_SELL_THRESHOLD", "-0.55"))
    min_buy_votes  = int(  env.get("ENSEMBLE_MIN_BUY_VOTES",  "2"))
    min_sell_votes = int(  env.get("ENSEMBLE_MIN_SELL_VOTES", "2"))

    htf_enabled    = env.get("HTF_BLOCK_ENABLED", "false").lower() == "true"
    htf_tf_min     = int(  env.get("HTF_BLOCK_TF_MINUTES",    "30"))
    htf_adx_period = int(  env.get("HTF_BLOCK_ADX_PERIOD",    "14"))
    htf_adx_thr    = float(env.get("HTF_BLOCK_ADX_THRESHOLD", "30.0"))
    htf_ov_enabled = env.get("HTF_MA_OVERRIDE_ENABLED", "true").lower() == "true"
    htf_ov_span    = int(  env.get("HTF_MA_OVERRIDE_SPAN",    "120"))
    htf_ov_pct     = float(env.get("HTF_MA_OVERRIDE_PCT",     "1.5"))

    sell_on_next_open = env.get("SELL_ON_NEXT_OPEN", "true").lower() == "true"
    add_buy_enabled = env.get("ADD_BUY_ENABLED", "true").lower() == "true"
    add_buy_frac    = float(env.get("ADD_BUY_FRACTION",         "0.20"))
    add_buy_max     = int(  env.get("ADD_BUY_MAX_COUNT",        "2"))
    add_buy_maxpos  = float(env.get("ADD_BUY_MAX_POSITION_PCT", "0.80"))
    inherit_stop    = env.get("ADD_BUY_INHERIT_INITIAL_STOP", "true").lower() == "true"
    cooldown_min    = int(  env.get("POST_STOPLOSS_COOLDOWN_MIN", "90"))
    pos_frac        = float(env.get("POSITION_FRACTION",        "0.70"))
    hard_stop_on    = env.get("ENGINE_HARD_STOP_ENABLED", "true").lower() == "true"
    hard_stop_pct_v = env.get("ENGINE_HARD_STOP_PCT", "")
    hard_stop_pct   = float(hard_stop_pct_v) if hard_stop_pct_v else None
    daily_max_loss  = float(env.get("DAILY_MAX_LOSS_PCT", "0"))

    print(f"[백테스트] 기간={period}  가중치={weights}")
    print(f"  BUY≥{buy_thr} {min_buy_votes}표 / SELL≤{sell_thr} {min_sell_votes}표  쿨다운={cooldown_min}분  포지션={pos_frac*100:.0f}%")
    print()

    hdr = f"{'종목':<14} {'스크리너':>8} {'수익률':>8} {'거래수':>6} {'승률':>7} {'MDD':>7} {'샤프':>7} {'손익비':>7}"
    sep = "=" * len(hdr)
    print(sep)
    print(hdr)
    print("-" * len(hdr))

    base_fn = _make_current()
    total_returns = []
    screener_map  = {sym: score for sym, score, _ in scored}

    for symbol in top_symbols:
        name = _sym_to_name(symbol)
        sc   = screener_map.get(symbol, 0)
        try:
            df = _download(symbol, period)
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
                hard_stop_enabled=hard_stop_on,
                hard_stop_pct=hard_stop_pct,
                daily_max_loss_pct=daily_max_loss,
            )
            pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
            label = f"{name}({symbol.split('.')[0]})"
            print(
                f"{label:<14} "
                f"{sc:>+8.1f} "
                f"{r.total_return_pct:>+8.2f}% "
                f"{r.trades:>6} "
                f"{r.win_rate:>6.1f}% "
                f"{r.max_drawdown_pct:>6.1f}% "
                f"{r.sharpe:>7.2f} "
                f"{pf:>7}"
            )
            total_returns.append(r.total_return_pct)
        except Exception as e:
            print(f"{name:<14} {sc:>+8.1f} 오류: {e}")

    if total_returns:
        avg = sum(total_returns) / len(total_returns)
        print(sep)
        print(f"{'평균':>22} {avg:>+8.2f}%")
        print(sep)


if __name__ == "__main__":
    main()
