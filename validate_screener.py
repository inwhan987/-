"""tech_score 검증 — 점수 순위 vs 실제 수익률 비교.

점수가 높은 종목이 실제로 수익률도 높은지 확인.
사용: python validate_screener.py
"""
from __future__ import annotations
import os, warnings, sys
warnings.filterwarnings("ignore")
os.environ.setdefault("DART_API_KEY", "dummy")

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util

HERE = Path(__file__).parent

# screener 모듈 로드
spec = importlib.util.spec_from_file_location("screener", HERE / "screener.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

NAMES = {
    "005930.KS":"삼성전자",    "000660.KS":"SK하이닉스",  "006400.KS":"삼성SDI",
    "009150.KS":"삼성전기",    "066570.KS":"LG전자",      "035720.KS":"카카오",
    "035420.KS":"NAVER",       "068270.KS":"셀트리온",    "207940.KS":"삼성바이오",
    "128940.KS":"한미약품",    "000100.KS":"유한양행",    "185750.KS":"종근당",
    "005380.KS":"현대차",      "000270.KS":"기아",        "051910.KS":"LG화학",
    "373220.KS":"LG에너지솔",  "247540.KS":"에코프로비엠","086520.KS":"에코프로",
    "105560.KS":"KB금융",      "055550.KS":"신한지주",    "086790.KS":"하나금융",
    "316140.KS":"우리금융",    "030200.KS":"KT",          "017670.KS":"SK텔레콤",
    "015760.KS":"한국전력",    "005490.KS":"POSCO홀딩스", "011070.KS":"LG이노텍",
    "010950.KS":"S-Oil",       "000720.KS":"현대건설",    "009540.KS":"HD한국조선해양",
    "329180.KS":"HD현대중공업",
}

def _get_returns(sym: str) -> dict:
    """yfinance에서 일봉 받아 20d/60d/90d 수익률 계산."""
    try:
        import yfinance as yf
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome")
        df = yf.download(sym, period="120d", interval="1d",
                         auto_adjust=True, progress=False, session=s)
        if df.empty:
            return {}
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                      for c in df.columns]
        close = df["close"].dropna()
        cur = float(close.iloc[-1])
        def ret(n):
            if len(close) >= n + 1:
                return round((cur / float(close.iloc[-n-1]) - 1) * 100, 1)
            return None
        return {"r20": ret(20), "r60": ret(60), "r90": ret(90)}
    except Exception:
        return {}

def _analyze(sym: str) -> dict:
    score, detail = m.tech_score(sym)
    rets = _get_returns(sym)
    return {"sym": sym, "score": score, "detail": detail, **rets}

candidates = list(NAMES.keys())
print(f"\n[검증] {len(candidates)}개 종목 tech_score + 실제 수익률 계산 중...")

results = []
with ThreadPoolExecutor(max_workers=6) as ex:
    futs = {ex.submit(_analyze, sym): sym for sym in candidates}
    for i, fut in enumerate(as_completed(futs), 1):
        r = fut.result()
        print(f"  [{i:2d}/{len(candidates)}] {NAMES.get(r['sym'],r['sym']):<12} "
              f"score={r['score']:>+6.1f}  "
              f"20d={str(r.get('r20','?')):>6}%  "
              f"60d={str(r.get('r60','?')):>6}%  "
              f"90d={str(r.get('r90','?')):>6}%")
        results.append(r)

results.sort(key=lambda x: -x["score"])

print(f"\n{'─'*80}")
print(f"{'종목':<12} {'점수':>6}  {'20일수익':>8}  {'60일수익':>8}  {'90일수익':>8}  주요지표")
print(f"{'─'*80}")

for r in results:
    name  = NAMES.get(r["sym"], r["sym"])
    d     = r["detail"]
    tags  = []
    if d.get("월봉EMA6","").startswith("위"): tags.append("EMA6↑")
    else: tags.append("EMA6↓")
    tags.append(f"RS{d.get('RS_KOSPI','?')[:5]}")
    tags.append(f"ADX{d.get('ADX','?')[:4]}")
    tags.append(d.get("Supertrend","?"))

    r20 = f"{r['r20']:>+.1f}%" if r.get("r20") is not None else "  N/A"
    r60 = f"{r['r60']:>+.1f}%" if r.get("r60") is not None else "  N/A"
    r90 = f"{r['r90']:>+.1f}%" if r.get("r90") is not None else "  N/A"

    print(f"{name:<12} {r['score']:>+6.1f}  {r20:>8}  {r60:>8}  {r90:>8}  {' | '.join(tags)}")

# 상위5 vs 하위5 평균 비교
top5  = [r for r in results[:5]  if r.get("r60") is not None]
bot5  = [r for r in results[-5:] if r.get("r60") is not None]
if top5 and bot5:
    avg_top = sum(r["r60"] for r in top5) / len(top5)
    avg_bot = sum(r["r60"] for r in bot5) / len(bot5)
    print(f"\n{'─'*80}")
    print(f"상위 5개 60일 평균: {avg_top:>+.1f}%")
    print(f"하위 5개 60일 평균: {avg_bot:>+.1f}%")
    print(f"격차: {avg_top - avg_bot:>+.1f}%p")

# 점수-수익률 상관계수
try:
    import numpy as np
    valid = [(r["score"], r["r60"]) for r in results if r.get("r60") is not None]
    if len(valid) >= 5:
        scores, r60s = zip(*valid)
        corr = float(np.corrcoef(scores, r60s)[0, 1])
        print(f"\ntech_score vs 60일수익률 상관계수: {corr:>+.3f}")
        if corr > 0.5:   print("  → 강한 양의 상관 ✅")
        elif corr > 0.3: print("  → 중간 양의 상관 ✅")
        elif corr > 0:   print("  → 약한 양의 상관")
        else:            print("  → 상관 없음 또는 역상관 ⚠️")
except Exception:
    pass
