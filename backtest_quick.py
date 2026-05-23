"""빠른 단순 백테스트 — 종목별 수익률/승률 출력.

사용:
  python backtest_quick.py [symbols] [period]
"""
from __future__ import annotations
import io, os, sys, tempfile, shutil

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import certifi
    _cert_dst = os.path.join(tempfile.gettempdir(), "cacert.pem")
    if not os.path.exists(_cert_dst):
        shutil.copy(certifi.where(), _cert_dst)
    os.environ.setdefault("CURL_CA_BUNDLE", _cert_dst)
    os.environ.setdefault("SSL_CERT_FILE", _cert_dst)
except Exception:
    pass

import yfinance as yf
import pandas as pd
from backtest_current import _load_env, _make_current, ATR_STOP_MAX_PCT
from stock_bot.backtest.engine import run_strategy

SYM_NAMES = {
    "005930.KS":"삼성전자","000660.KS":"SK하이닉스","006400.KS":"삼성SDI",
    "009150.KS":"삼성전기","066570.KS":"LG전자","035720.KS":"카카오",
    "035420.KS":"NAVER","068270.KS":"셀트리온","207940.KS":"삼성바이오",
    "005380.KS":"현대차","000270.KS":"기아","051910.KS":"LG화학",
    "105560.KS":"KB금융","055550.KS":"신한지주","086790.KS":"하나금융",
    "316140.KS":"우리금융","030200.KS":"KT","017670.KS":"SK텔레콤",
    "005490.KS":"POSCO홀딩스","011070.KS":"LG이노텍","329180.KS":"HD현대중공업",
    "009540.KS":"HD한국조선해양","086520.KS":"에코프로","247540.KS":"에코프로비엠",
    # 스크리너 추가 종목
    "028260.KS":"삼성물산","021240.KS":"코웨이","033780.KS":"KT&G",
    "064400.KS":"LG씨엔에스","004370.KS":"농심","036570.KS":"NC소프트",
    "012510.KS":"더존비즈온","088350.KS":"한화생명","010120.KS":"LS ELECTRIC",
    "298040.KS":"효성중공업","000990.KS":"DB하이텍","111770.KS":"영원무역",
    "005850.KS":"에스엘","007810.KS":"코리아써키트","062040.KS":"산일전기",
    "004170.KS":"신세계","483650.KS":"달바글로벌","032830.KS":"삼성생명",
    "161390.KS":"한국타이어앤테크놀로지","383220.KS":"F&F",
}

def _download(symbol, period):
    df = yf.download(symbol, period=period, interval="5m",
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"no data: {symbol}")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.dropna(subset=["close"]).copy()
    df.index = pd.to_datetime(df.index)
    return df

def _run(df, sym, fn, env):
    return run_strategy(
        df, fn, sym, stop_loss_pct=ATR_STOP_MAX_PCT,
        enable_add_buy=env.get("ADD_BUY_ENABLED","true").lower()=="true",
        add_buy_fraction=float(env.get("ADD_BUY_FRACTION","0.20")),
        add_buy_max_count=int(env.get("ADD_BUY_MAX_COUNT","2")),
        add_buy_max_position_pct=float(env.get("ADD_BUY_MAX_POSITION_PCT","0.80")),
        inherit_initial_stop=env.get("ADD_BUY_INHERIT_INITIAL_STOP","true").lower()=="true",
        post_stoploss_cooldown_min=int(env.get("POST_STOPLOSS_COOLDOWN_MIN","30")),
        initial_position_fraction=float(env.get("POSITION_FRACTION","0.40")),
        bar_minutes=5,
        sell_on_next_open=env.get("SELL_ON_NEXT_OPEN","true").lower()=="true",
    )

def main():
    symbols_str = sys.argv[1] if len(sys.argv) > 1 else "005930.KS,000660.KS"
    period      = sys.argv[2] if len(sys.argv) > 2 else "60d"
    label       = sys.argv[3] if len(sys.argv) > 3 else ""
    symbols = [s.strip() for s in symbols_str.split(",")]

    env = _load_env()
    fn  = _make_current()

    title = f"백테스트 결과  {label}  period={period}"
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

    print(f"  데이터 다운로드 중...")
    dfs = {}
    for sym in symbols:
        print(f"    {sym} ...", end=" ", flush=True)
        try:
            dfs[sym] = _download(sym, period)
            print(f"{len(dfs[sym])}봉")
        except Exception as e:
            print(f"ERR: {e}")

    print(f"\n  {'종목':<14} {'이름':<12} {'수익률':>8}  {'승률':>7}  {'거래수':>5}  {'PF':>6}  {'MDD':>7}")
    print(f"  {'-'*62}")

    rets = []
    for sym in symbols:
        name = SYM_NAMES.get(sym, sym)
        if sym not in dfs:
            print(f"  {sym:<14} {name:<12}  {'N/A':>8}")
            continue
        try:
            r = _run(dfs[sym], sym, fn, env)
            pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else " inf"
            print(f"  {sym:<14} {name:<12} {r.total_return_pct:>+7.2f}%  {r.win_rate:>6.1f}%  {r.trades:>5}T  {pf:>6}  {r.max_drawdown_pct:>6.1f}%")
            rets.append(r.total_return_pct)
        except Exception as e:
            print(f"  {sym:<14} {name:<12}  ERR: {e}")

    avg = sum(rets)/len(rets) if rets else 0.0
    print(f"  {'-'*62}")
    print(f"  {'평균':>28} {avg:>+7.2f}%")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
