"""다음 금융(finance.daum.net) 거래대금 순위 API — 대장주 선별 유니버스 소스.

매경(mk_quant) 대비 이점: 실시간 갱신(다음 웹페이지에 보이는 그 값 그대로).
매경은 SSR 페이지 캐시가 있어 장중 갱신이 몇 분~수십분 지연되는 문제 확인.

엔드포인트: GET /api/trend/trade_volume
  · fieldName=accTradePrice, order=desc → 거래대금 내림차순 (서버 정렬)
  · market=KOSPI|KOSDAQ, perPage=100, page=N
  · 응답: {data: [{symbolCode:'A005930', name, tradePrice, accTradeVolume,
                  accTradePrice, changeRate, ...}], totalPages, totalCount}

NXT 포함 여부: 다음 금융은 KRX 데이터 피드만 받는 것으로 알려짐(공식 문서 없음).
장중에 KIS J(KRX-only) 값과 나란히 대조해 확인할 것 — 유의미하게 크면 NXT 포함.

동작:
  · fetch_ranking(top_n=100, stock_only=True) — 시장당 top_n 채우기.
  · ETF/ETN/우선주 제외 후 개별주가 top_n 도달할 때까지 페이지 확장(최대 6페이지).
  · 반환 스키마: code,name,price,change_pct,volume,value_won,market_cap(0),market.
    naver_quant.fetch_ranking / mk_quant.fetch_ranking / kis_quant.fetch_ranking 드롭인.

시총 랭킹은 이 모듈에 없음 — 실시간 아닌 자료라 mk_quant.fetch_marketcap_map 유지.
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd
import requests

from naver_quant import _HDR, _is_common_stock

_URL = "https://finance.daum.net/api/trend/trade_volume"
_REFERER = "https://finance.daum.net/domestic/volume"
_MAX_PAGES = 6  # 시장당 600종목까지 — 02시 프리페치가 시총≥1000억 전체를 걸러야 해서 확장(2026-08-12)


def _fetch_market(market: str, top_n: int, stock_only: bool) -> pd.DataFrame:
    """단일 시장(KOSPI|KOSDAQ)에서 stock_only 필터 후 top_n 개 채우기."""
    kept: list[dict] = []
    seen: set[str] = set()
    hdr = dict(_HDR)
    hdr["Referer"] = _REFERER
    for page in range(1, _MAX_PAGES + 1):
        params = {
            "page": page,
            "perPage": 100,
            "fieldName": "accTradePrice",
            "order": "desc",
            "market": market,
            "pagination": "true",
        }
        try:
            r = requests.get(_URL, params=params, headers=hdr, timeout=10)
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception as e:
            print(f"  [다음 {market} p{page} 실패] {e}")
            break
        if not data:
            break
        for row in data:
            sym = str(row.get("symbolCode") or "")
            # symbolCode 예: 'A005930' → 6자리 종목코드로 정규화
            code = sym[1:] if sym.startswith("A") and len(sym) == 7 else sym
            if len(code) != 6 or not code.isdigit():
                continue
            if code in seen:
                continue
            name = str(row.get("name") or "").strip()
            if stock_only and not _is_common_stock(code, name):
                continue
            try:
                price = float(row.get("tradePrice") or 0)
                vol = float(row.get("accTradeVolume") or 0)
                val = float(row.get("accTradePrice") or 0)
                chg = float(row.get("changeRate") or 0) * 100.0  # 0.0032 → 0.32%
            except (TypeError, ValueError):
                continue
            seen.add(code)
            kept.append({
                "code": code, "name": name, "price": price,
                "change_pct": chg, "volume": vol,
                "value_won": val,
                "market_cap": 0.0,  # daum 랭킹엔 미제공 — leader_finder 가 시총캐시로 주입
                "market": market,
            })
            if len(kept) >= top_n:
                break
        if len(kept) >= top_n:
            break
    return pd.DataFrame(kept)


def fetch_ranking(top_n: int = 100, stock_only: bool = True,
                  markets: Iterable[str] = ("KOSPI", "KOSDAQ")) -> pd.DataFrame:
    """다음 거래대금 상위. 시장별 stock_only 필터 후 개별주 top_n 개 채우기.

    - stock_only=True: ETF/ETN/우선주 제외한 개별주만.
    - top_n: 시장당 개수(코스피 top_n + 코스닥 top_n).
    - markets: 기본 ("KOSPI", "KOSDAQ"). 소문자로 넣어도 대문자 변환.
    """
    frames = []
    for m in markets:
        mkt = m.upper()
        df = _fetch_market(mkt, top_n=top_n, stock_only=stock_only)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("code")
    return df.sort_values("value_won", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    df = fetch_ranking(top_n=100, stock_only=True)
    print(f"거래대금 수집 {len(df)}종목")
    print(df.head(20)[["code", "name", "price", "change_pct", "value_won", "market"]]
          .to_string(index=False))
