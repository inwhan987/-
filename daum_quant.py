"""다음 금융(finance.daum.net) 거래대금 순위 API — 대장주 선별 유니버스 소스.

매경(mk_quant) 대비 이점: 실시간 갱신(다음 웹페이지에 보이는 그 값 그대로).
매경은 SSR 페이지 캐시가 있어 장중 갱신이 몇 분~수십분 지연되는 문제 확인.

엔드포인트: GET /api/trend/trade_volume
  · fieldName=accTradePrice, order=desc → 거래대금 내림차순 (서버 정렬)
  · market=KOSPI|KOSDAQ, perPage=100, page=N
  · 응답: {data: [{symbolCode:'A005930', name, tradePrice, accTradeVolume,
                  accTradePrice, change(RISE|FALL|EVEN), changeRate(절대값), ...}],
                  totalPages, totalCount}

NXT 포함 여부: 다음 금융은 KRX 데이터 피드만 받는 것으로 알려짐(공식 문서 없음).
장중에 KIS J(KRX-only) 값과 나란히 대조해 확인할 것 — 유의미하게 크면 NXT 포함.

동작:
  · fetch_ranking(top_n=100, stock_only=True) — 시장당 top_n 채우기.
  · ETF/ETN/우선주 제외 후 개별주가 top_n 도달할 때까지 페이지 확장(최대 6페이지).
  · 반환 스키마: code,name,price,change_pct,volume,value_won,market_cap(0),market.
    naver_quant.fetch_ranking / mk_quant.fetch_ranking / kis_quant.fetch_ranking 드롭인.

시총 랭킹: fetch_marketcap_map() — /api/trend/market_capitalization.
  · marketCap(원) + listedShareCount 를 그대로 준다.
  · 매경(mk_quant) 대비: 코스피 2,480 / 코스닥 1,821 전수(ETF·우선주 포함
    totalCount)라 9xxxxx(950 외국주권 · 900 중국기업)까지 빠짐없이 들어온다.
    매경 시총 랭킹은 이 종목군을 통째로 누락해 영원히 miss 였다(2026-08-28).
  · 44요청 11초 (매경 50페이지 크롤 ~24초). mk_quant 는 폴백으로 유지.
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
                # ★ changeRate 는 **부호 없는 절대값**이다(다음 API 스펙).
                #   방향은 별도 필드 change 에 RISE/FALL/EVEN 으로 온다.
                #   부호를 안 붙이면 하락 종목이 전부 상승으로 보여 등락률 게이트
                #   (rise_min)를 통과하고, 폭락일에 대장주로 뽑힌다.
                #   2026-08-19 실사고: SK하이닉스 -8.3% → +8.84% 로 뒤집혀 1위 선별,
                #   삼성전자 매수 진입. (2026-08-11 다음 소스 전환 때 유입)
                chg = float(row.get("changeRate") or 0) * 100.0  # 0.0032 → 0.32%
                if str(row.get("change") or "").upper() == "FALL":
                    chg = -chg
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


# ── 시가총액 맵 ──────────────────────────────────────────────────────
_MKTCAP_URL = "https://finance.daum.net/api/trend/market_capitalization"
_MKTCAP_REFERER = "https://finance.daum.net/domestic/all_stocks"
_MKTCAP_MAX_PAGES = 40  # perPage=100 · 실측 코스피 25p · 코스닥 19p

# mk_quant 와 동일한 계약 — 부분 크롤이면 True (호출자가 저장을 건너뛴다).
LAST_MKTCAP_INCOMPLETE: bool = False


def fetch_marketcap_map(stock_only: bool = True,
                        markets: Iterable[str] = ("KOSPI", "KOSDAQ"),
                        max_pages: int = _MKTCAP_MAX_PAGES,
                        ) -> dict[str, float]:
    """다음 시가총액 랭킹 → {code: market_cap_won}. mk_quant 드롭인.

    페이지 하나라도 실패하면 LAST_MKTCAP_INCOMPLETE=True 로 알린다 —
    호출자(leader_finder._load_mktcap_cache)가 반쪽 크롤을 캐시에 저장하지
    않고 직전 캐시로 폴백한다.
    """
    global LAST_MKTCAP_INCOMPLETE
    LAST_MKTCAP_INCOMPLETE = False
    hdr = dict(_HDR)
    hdr["Referer"] = _MKTCAP_REFERER
    result: dict[str, float] = {}
    for market in markets:
        mk = str(market).upper()
        page = 1
        while page <= max_pages:
            params = {"page": page, "perPage": 100, "market": mk, "pagination": "true"}
            try:
                r = requests.get(_MKTCAP_URL, params=params, headers=hdr, timeout=10)
                r.raise_for_status()
                body = r.json()
            except Exception as e:
                print(f"  [다음 시총 {mk} p{page} 실패] {e}")
                LAST_MKTCAP_INCOMPLETE = True
                break
            rows = body.get("data") or []
            if not rows:
                break
            for row in rows:
                sym = str(row.get("symbolCode") or "")
                code = sym[1:] if sym.startswith("A") and len(sym) == 7 else sym
                if len(code) != 6 or not code.isdigit():
                    continue
                name = str(row.get("name") or "").strip()
                if stock_only and not _is_common_stock(code, name):
                    continue
                try:
                    cap = float(row.get("marketCap") or 0)
                except (TypeError, ValueError):
                    continue
                if cap > 0:
                    result[code] = cap
            total_pages = int(body.get("totalPages") or 0)
            if total_pages and page >= total_pages:
                break
            page += 1
        else:
            # max_pages 소진 — totalPages 에 못 미쳤을 수 있다
            LAST_MKTCAP_INCOMPLETE = True
    return result

