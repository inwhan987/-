"""장분석 + 섹터분석 — 스크리너 실행(평일 08:00) 직전 시장 레짐과 최강 섹터 산출.

- 레짐(장분석): KODEX 200(069500)을 코스피 대용치로 사용, 종가 vs N일선 → 상승/하락장.
  (pykrx 지수 OHLCV 는 KRX 로그인 필요로 불안정 → 일반 ETF 종목으로 대체)
- 섹터(섹터분석): 거래대금 상위 종목들의 N거래일 수익률을 네이버 업종별로 평균 →
  가장 강한 섹터 1개. 그 업종명을 screener --sector 로 그대로 넘기면 부분일치로 매칭된다.

사용:
  python market_analysis.py                 # 레짐 + 섹터 랭킹 출력
  python market_analysis.py --rs-days 20 --top-sectors 5
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

KOSPI_PROXY  = "069500"  # KODEX 200    (코스피 대용치)
KOSDAQ_PROXY = "229200"  # KODEX 코스닥150 (코스닥 대용치)


def _ohlcv_closes(code: str, days: int):
    """pykrx 일봉 종가 시리즈(최근 days일). 실패 시 최대 3회 재시도 후 None."""
    from pykrx import stock as krx
    end = datetime.now()
    start = end - timedelta(days=days)
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    for attempt in range(3):
        try:
            df = krx.get_market_ohlcv_by_date(s, e, code)
            if df is not None and not df.empty and "종가" in df.columns:
                cl = df["종가"].astype(float)
                cl = cl[cl > 0]
                if len(cl) >= 2:
                    return cl
        except BaseException:  # pykrx 내부 sys.exit 방어
            pass
        if attempt < 2:
            time.sleep(0.4 * (attempt + 1))
    return None


def _regime_one(code: str, ma: int) -> dict | None:
    """단일 지수 ETF 종가 vs ma일선 → {'close','ma','gap_pct'} 또는 None."""
    cl = _ohlcv_closes(code, days=ma * 2 + 15)
    if cl is None or len(cl) < ma:
        return None
    last = float(cl.iloc[-1])
    mav = float(cl.tail(ma).mean())
    gap = (last / mav - 1) * 100 if mav > 0 else 0.0
    return {"close": last, "ma": mav, "gap_pct": gap}


def market_regime(ma: int = 20) -> dict:
    """코스피(KODEX200)+코스닥(KODEX코스닥150) 이격도 평균으로 레짐 판정.

    두 지수의 (종가/ma일선 -1)%를 평균 → 음수면 하락장, 0 이상이면 상승장.
    한쪽 데이터만 있으면 그 한쪽으로 판정. 둘 다 실패면 unknown.
    """
    ks = _regime_one(KOSPI_PROXY, ma)
    kq = _regime_one(KOSDAQ_PROXY, ma)
    gaps = [r["gap_pct"] for r in (ks, kq) if r is not None]
    if not gaps:
        return {"regime": "unknown", "gap_pct": 0.0, "kospi": ks, "kosdaq": kq}
    avg_gap = sum(gaps) / len(gaps)
    return {"regime": "up" if avg_gap >= 0 else "down",
            "gap_pct": avg_gap, "kospi": ks, "kosdaq": kq}


def _stock_rs_and_sector(code: str, rs_days: int) -> tuple[float | None, str]:
    """(N거래일 수익률 %, 네이버 업종명). 실패 시 (None, '')."""
    from screener import _naver_industry
    cl = _ohlcv_closes(code, days=rs_days * 2 + 20)
    rs = None
    if cl is not None and len(cl) >= rs_days + 1:
        base = float(cl.iloc[-(rs_days + 1)])
        if base > 0:
            rs = (float(cl.iloc[-1]) / base - 1) * 100
    sector = _naver_industry(code) if rs is not None else ""
    return rs, sector


def sector_ranking(rs_days: int = 20, universe_top: int = 200,
                   min_stocks: int = 5, workers: int = 8) -> list[dict]:
    """거래대금 상위 종목의 N거래일 수익률을 업종별 평균 → 강도 내림차순 랭킹.

    반환: [{'sector', 'avg_rs', 'count'}], min_stocks개 이상인 업종만.
    """
    from leader_finder import fetch_ranking
    rank_df = fetch_ranking(top_n=universe_top)
    if rank_df is None or rank_df.empty:
        return []
    codes = [str(c) for c in rank_df["code"].tolist()]

    buckets: dict[str, list[float]] = defaultdict(list)
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futs = {exe.submit(_stock_rs_and_sector, c, rs_days): c for c in codes}
        for f in as_completed(futs):
            try:
                rs, sector = f.result()
            except Exception:
                continue
            if rs is not None and sector:
                buckets[sector].append(rs)

    scored = [{"sector": s, "avg_rs": sum(v) / len(v), "count": len(v)}
              for s, v in buckets.items() if len(v) >= min_stocks]
    scored.sort(key=lambda x: x["avg_rs"], reverse=True)
    return scored


def analyze(rs_days: int = 20, universe_top: int = 200,
            min_stocks: int = 5, ma: int = 20, workers: int = 8) -> dict:
    """레짐 + 최강 섹터 종합. app.py 스크리너 프리스텝에서 호출."""
    regime = market_regime(ma=ma)
    ranking = sector_ranking(rs_days=rs_days, universe_top=universe_top,
                             min_stocks=min_stocks, workers=workers)
    top_sector = ranking[0]["sector"] if ranking else ""
    return {"regime": regime, "top_sector": top_sector, "ranking": ranking}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rs-days", type=int, default=20, help="섹터 강도 산정 기간(거래일)")
    p.add_argument("--ma", type=int, default=20, help="레짐 판정 이동평균(거래일)")
    p.add_argument("--universe-top", type=int, default=200, help="분석 대상 거래대금 상위 N")
    p.add_argument("--min-stocks", type=int, default=5, help="업종당 최소 종목 수")
    p.add_argument("--top-sectors", type=int, default=8, help="출력할 섹터 수")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--json", action="store_true",
                   help="결과를 JSON 한 줄로 출력(app.py 연동용)")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.json:
        # --json 모드: 어떤 오류가 나도 반드시 마커+JSON 출력 → app.py가 안전하게 파싱
        import json, traceback
        try:
            res = analyze(rs_days=args.rs_days, universe_top=args.universe_top,
                          min_stocks=args.min_stocks, ma=args.ma, workers=args.workers)
        except Exception as _e:
            res = {
                "regime":     {"regime": "unknown", "gap_pct": 0.0, "kospi": None, "kosdaq": None},
                "top_sector": "",
                "ranking":    [],
                "error":      f"{_e}\n{traceback.format_exc()[-600:]}",
            }
        print("ANALYSIS_JSON_BEGIN")
        print(json.dumps(res, ensure_ascii=False))
        print("ANALYSIS_JSON_END")
        return

    res = analyze(rs_days=args.rs_days, universe_top=args.universe_top,
                  min_stocks=args.min_stocks, ma=args.ma, workers=args.workers)

    reg = res["regime"]
    reg_kr = {"up": "상승장", "down": "하락장", "unknown": "판정불가"}[reg["regime"]]
    def _one(label, r):
        return (f"{label} {r['gap_pct']:+.1f}%" if r else f"{label} N/A")
    ks_s = _one("코스피", reg.get("kospi"))
    kq_s = _one("코스닥", reg.get("kosdaq"))
    print(f"\n📈 장분석: {reg_kr}  (평균 {reg['gap_pct']:+.1f}% vs {args.ma}일선 "
          f"| {ks_s}, {kq_s})")
    print(f"\n🏅 섹터 강도 ({args.rs_days}거래일 수익률, 거래대금 상위 {args.universe_top})")
    print("-" * 48)
    for i, s in enumerate(res["ranking"][:args.top_sectors], 1):
        mark = "★" if i == 1 else " "
        print(f"  {mark}{i:>2} {s['sector']:<22} {s['avg_rs']:>+7.1f}%  ({s['count']}종목)")
    print(f"\n→ 최강 섹터: {res['top_sector'] or '(없음)'}")


if __name__ == "__main__":
    main()
