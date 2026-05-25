"""tech_score 검증 — KOSPI 시총 상위 100개 종목, 점수 vs 실제 수익률 비교.

사용: python validate_screener_100.py
"""
from __future__ import annotations
import os, warnings, sys
warnings.filterwarnings("ignore")
os.environ.setdefault("DART_API_KEY", "dummy")

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util

HERE = Path(__file__).parent

spec = importlib.util.spec_from_file_location("screener", HERE / "screener.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def _get_kospi_top100() -> dict[str, str]:
    """pykrx로 KOSPI 시총 상위 100개 종목 코드+이름 반환."""
    try:
        from pykrx import stock as krx
        from datetime import datetime, timedelta

        d = datetime.now()
        for _ in range(7):
            if d.weekday() < 5:
                break
            d -= timedelta(days=1)
        date_str = d.strftime("%Y%m%d")

        df = krx.get_market_cap(date_str, market="KOSPI")
        df = df.sort_values("시가총액", ascending=False).head(100)

        result = {}
        for code in df.index:
            name = krx.get_market_ticker_name(code)
            result[f"{code}.KS"] = name
        return result
    except Exception as e:
        print(f"[!] pykrx 실패 ({e}), 하드코딩 폴백 사용")
        return {sym: sym.split(".")[0] for sym in m._FALLBACK_KOSPI[:100]}


def _get_returns(sym: str) -> dict:
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


print("[종목 목록 조회 중...]")
NAMES = _get_kospi_top100()
candidates = list(NAMES.keys())
print(f"[검증] {len(candidates)}개 종목 tech_score + 실제 수익률 계산 중...")

results = []
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(_analyze, sym): sym for sym in candidates}
    for i, fut in enumerate(as_completed(futs), 1):
        r = fut.result()
        name = NAMES.get(r["sym"], r["sym"])
        print(f"  [{i:3d}/{len(candidates)}] {name:<12} "
              f"score={r['score']:>+6.1f}  "
              f"20d={str(r.get('r20','?')):>6}%  "
              f"60d={str(r.get('r60','?')):>6}%")
        results.append(r)

results.sort(key=lambda x: -x["score"])

print(f"\n{'─'*90}")
print(f"{'종목':<12} {'점수':>6}  {'20일':>7}  {'60일':>7}  {'90일':>7}  주요지표")
print(f"{'─'*90}")

for r in results:
    name = NAMES.get(r["sym"], r["sym"])
    d    = r["detail"]
    tags = []
    ema6 = d.get("월봉EMA6","")
    tags.append("EMA6↑" if ema6.startswith("위") else "EMA6↓")
    rs = d.get("RS_KOSPI","?"); tags.append(f"RS{rs[:6]}")
    rs60 = d.get("RS60_KOSPI","?"); tags.append(f"RS60:{rs60[:5]}")
    adx = d.get("ADX","?");     tags.append(f"ADX{adx[:4]}")
    tags.append(d.get("Supertrend","?")[:2])

    r20 = f"{r['r20']:>+.1f}%" if r.get("r20") is not None else "  N/A"
    r60 = f"{r['r60']:>+.1f}%" if r.get("r60") is not None else "  N/A"
    r90 = f"{r['r90']:>+.1f}%" if r.get("r90") is not None else "  N/A"
    print(f"{name:<12} {r['score']:>+6.1f}  {r20:>7}  {r60:>7}  {r90:>7}  {' | '.join(tags)}")

# 상위10 vs 하위10 비교
top10 = [r for r in results[:10]  if r.get("r60") is not None]
bot10 = [r for r in results[-10:] if r.get("r60") is not None]
if top10 and bot10:
    avg_top = sum(r["r60"] for r in top10) / len(top10)
    avg_bot = sum(r["r60"] for r in bot10) / len(bot10)
    print(f"\n{'─'*90}")
    print(f"상위 10개 60일 평균: {avg_top:>+.1f}%")
    print(f"하위 10개 60일 평균: {avg_bot:>+.1f}%")
    print(f"격차: {avg_top - avg_bot:>+.1f}%p")

# 분위별 수익률 (5분위)
valid = [(r["score"], r["r60"]) for r in results if r.get("r60") is not None]
valid.sort(key=lambda x: -x[0])
n = len(valid)
q = n // 5
print(f"\n[5분위 분석] (각 ~{q}개)")
for qi in range(5):
    grp = valid[qi*q:(qi+1)*q]
    avg = sum(x[1] for x in grp) / len(grp) if grp else 0
    sc_avg = sum(x[0] for x in grp) / len(grp) if grp else 0
    print(f"  Q{qi+1} (점수평균 {sc_avg:>+.1f}): 60일 평균수익 {avg:>+.1f}%  ({len(grp)}개)")

# 상관계수
try:
    import numpy as np
    if len(valid) >= 10:
        scores, r60s = zip(*valid)
        corr = float(np.corrcoef(scores, r60s)[0, 1])
        print(f"\ntech_score vs 60일수익률 상관계수: {corr:>+.3f}  ({len(valid)}개 종목)")
        if corr > 0.5:   print("  → 강한 양의 상관 ✅")
        elif corr > 0.3: print("  → 중간 양의 상관 ✅")
        elif corr > 0:   print("  → 약한 양의 상관")
        else:            print("  → 상관 없음 또는 역상관 ⚠️")
except Exception:
    pass
