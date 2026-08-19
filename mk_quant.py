"""매일경제(stock.mk.co.kr) 거래대금 순위 크롤러 — 대장주 선별 유니버스 소스.

네이버 sise_quant 는 KRX/NXT 가 페이지가 분리돼 있어 unified 크롤링(4콜)이 필요하고,
NXT 상위 100 밖 종목은 편측 관측으로 과소계상된다. 매경은 SSR·인증불요·페이지네이션
지원(page=N)에 KRX 통합 거래대금(정규시장 기준)을 이미 정렬한 상태로 제공한다.
NXT 는 매경 페이지에서 지원하지 않음(사용자 확인: 넥스트 거래대금 반영 X) — KRX 만
쓰는 정책과 부합한다.

컬럼(오늘 확인, 2026-08-10):
  span.no / a[href="/price/home/KR7XXXXXXX"] / span.price / span.fl {up|down}
  / span.vol / span.price_hun(거래대금 백만원) / span.price_y(전일거래대금 백만원)

동작:
  · fetch_ranking(top_n=100, stock_only=True) — KOSPI+KOSDAQ 각각 top_n 채우기.
  · ETF/ETN/우선주 제외 후 순수 개별주가 top_n 도달할 때까지 페이지 확장(최대 5페이지).
  · 반환 스키마: code,name,price,change_pct,volume,value_won,market_cap(0),market.
    naver_quant.fetch_ranking / kis_quant.fetch_ranking 드롭인.
"""
from __future__ import annotations

import re
import time
from typing import Iterable

import pandas as pd
import requests

from naver_quant import _HDR, _is_common_stock

_BASE = "https://stock.mk.co.kr/domestic/ranking/amount"
_MKTCAP_BASE = "https://stock.mk.co.kr/domestic/ranking/market_cap"
_MAX_PAGES = 5
_MKTCAP_MAX_PAGES = 50  # 시장당 최대(페이지당 50종목) — KOSPI 46p/KOSDAQ 34p 전체 커버(2026-08-12 실측).
                        # 25p 캡이던 시절 KOSPI 1000+·KOSDAQ 400+ 종목 누락(회전율 계산 불가로 이어짐).
                        # 빈 페이지 만나면 루프가 즉시 break 하므로 여유 캡이어도 실호출 비용은 안 늘어남.

# 한 tr 안에서 필요한 span 을 한 번에 뽑는 정규식.
_ISIN_RE = re.compile(r"/price/home/KR7(\d{6})\d")
_NAME_RE = re.compile(r'<span class="name">\s*<a[^>]*>\s*([^<]+?)\s*</a>')
_PRICE_RE = re.compile(r'<span class="price">([\d,]+)</span>')
_CHG_RE = re.compile(r'<span class="fl (up|down)">([\-\d.,]+)</span>')
_VOL_RE = re.compile(r'<span class="vol">([\d,]+)</span>')
_VAL_RE = re.compile(r'<span class="price_hun">([\d,]+)</span>')
_MKTCAP_RE = re.compile(r'<span class="price_mi">([\d,]+)</span>')  # 시가총액(백만원)
_TR_RE = re.compile(r"<tr[^>]*>([\s\S]*?)</tr>", re.IGNORECASE)


# ── 페이지 조회 재시도 (2026-08-19) ──────────────────────────────────
# DNS 한 번 튀면 그 시장의 나머지 페이지가 통째로 날아간다. 실측(08-19 12:54)
# 으로 kosdaq p18 의 NameResolutionError 하나가 ~800종목을 삼켰고, 그 반쪽
# 결과가 "오늘자" 시총 캐시로 저장돼 하루 종일 쓰였다. 캐시가 하루 1회
# (새벽 프리페치) 만들어지도록 바뀐 뒤로 그 1회의 실패 비용이 더 커졌다.
_RETRY_TRIES = 3
_RETRY_BACKOFF = (1.5, 3.0)  # 1차 실패 후 1.5초, 2차 실패 후 3.0초

# 직전 fetch_marketcap_map() 이 페이지 실패로 중단됐는지. 호출자(leader_finder)
# 가 "불완전한 크롤은 오늘자 캐시로 저장하지 않는다" 판단에 쓴다.
LAST_MKTCAP_INCOMPLETE: bool = False


def _get(url: str, label: str) -> str | None:
    """GET 재시도. 전부 실패하면 None (호출자가 중단 사유로 해석)."""
    for i in range(1, _RETRY_TRIES + 1):
        try:
            r = requests.get(url, headers=_HDR, timeout=10)
            r.raise_for_status()
            return r.text
        except Exception as e:
            if i >= _RETRY_TRIES:
                print(f"  [매경 {label} 실패] {e} (재시도 {_RETRY_TRIES}회 소진)")
                return None
            wait = _RETRY_BACKOFF[i - 1]
            print(f"  [매경 {label} 재시도 {i}/{_RETRY_TRIES - 1}] {e} — {wait}초 후")
            time.sleep(wait)
    return None


def _to_float(s: str) -> float:
    try:
        return float(s.replace(",", "").replace("+", ""))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _parse_page(html: str, market: str) -> list[dict]:
    rows: list[dict] = []
    for m in _TR_RE.finditer(html):
        seg = m.group(1)
        isin = _ISIN_RE.search(seg)
        if not isin:
            continue
        code = isin.group(1)
        name_m = _NAME_RE.search(seg)
        val_m = _VAL_RE.search(seg)
        if not name_m or not val_m:
            continue
        name = name_m.group(1).strip()
        # 거래대금: price_hun(백만원) × 1e6
        value_won = _to_float(val_m.group(1)) * 1_000_000
        price = _to_float(_PRICE_RE.search(seg).group(1)) if _PRICE_RE.search(seg) else 0.0
        vol = _to_float(_VOL_RE.search(seg).group(1)) if _VOL_RE.search(seg) else 0.0
        chg = 0.0
        chg_m = _CHG_RE.search(seg)
        if chg_m:
            sign = -1.0 if chg_m.group(1) == "down" else 1.0
            v = _to_float(chg_m.group(2))
            # 값에 이미 '-' 부호가 붙어오면 그대로, 없으면 클래스 기반 부호 적용.
            chg = v if v < 0 else sign * abs(v)
        rows.append({
            "code": code, "name": name, "price": price,
            "change_pct": chg, "volume": vol,
            "value_won": value_won,
            "market_cap": 0.0,  # 매경 SSR 미제공(leader_finder 는 mktcap==0 시 게이트 pass)
            "market": market,
        })
    return rows


def _fetch_market(market_type: str, market_label: str, top_n: int,
                  stock_only: bool) -> pd.DataFrame:
    """단일 시장(kospi|kosdaq) 에서 stock_only 필터 후 top_n 개 채우기."""
    kept: list[dict] = []
    seen: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
        url = f"{_BASE}?type={market_type}&page={page}" if page > 1 else f"{_BASE}?type={market_type}"
        html = _get(url, f"{market_label} p{page}")
        if html is None:
            break
        parsed = _parse_page(html, market_label)
        if not parsed:
            break
        added = 0
        for row in parsed:
            if row["code"] in seen:
                continue
            if stock_only and not _is_common_stock(row["code"], row["name"]):
                continue
            seen.add(row["code"])
            kept.append(row)
            added += 1
            if len(kept) >= top_n:
                break
        if len(kept) >= top_n:
            break
        # 페이지 반환 종목이 0이면 다음 페이지도 없음
        if added == 0 and not parsed:
            break
    return pd.DataFrame(kept)


def fetch_ranking(top_n: int = 100, stock_only: bool = True,
                  markets: Iterable[str] = ("kospi", "kosdaq")) -> pd.DataFrame:
    """매경 거래대금 상위. 시장별 stock_only 필터 후 개별주 top_n 개 채우기.

    - stock_only=True: ETF/ETN/우선주 제외한 개별주만.
    - top_n: 시장당 개수(코스피 top_n + 코스닥 top_n).
    - markets: 순회할 시장 리스트. 기본 ("kospi", "kosdaq").
    """
    _map = {"kospi": "KOSPI", "kosdaq": "KOSDAQ"}
    frames = []
    for m in markets:
        label = _map.get(m, m.upper())
        df = _fetch_market(m, label, top_n=top_n, stock_only=stock_only)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("code")
    return df.sort_values("value_won", ascending=False).reset_index(drop=True)


def _parse_mktcap_page(html: str) -> list[tuple[str, str, float]]:
    """시총 페이지 파싱 → [(code, name, market_cap_won), ...]"""
    out: list[tuple[str, str, float]] = []
    for m in _TR_RE.finditer(html):
        seg = m.group(1)
        isin = _ISIN_RE.search(seg)
        cap_m = _MKTCAP_RE.search(seg)
        name_m = _NAME_RE.search(seg)
        if not (isin and cap_m and name_m):
            continue
        cap_won = _to_float(cap_m.group(1)) * 1_000_000  # 백만원 → 원
        out.append((isin.group(1), name_m.group(1).strip(), cap_won))
    return out


def fetch_marketcap_map(stock_only: bool = True,
                        markets: Iterable[str] = ("kospi", "kosdaq"),
                        max_pages: int = _MKTCAP_MAX_PAGES) -> dict[str, float]:
    """매경 시가총액 랭킹 크롤링 → {code: market_cap_won}.

    장 시작 전 1회 캐시(하루 유효)를 목적으로 leader_finder 가 호출한다.
    코스피·코스닥 각각 max_pages 만큼 크롤링해 코드→시총(원) 딕셔너리 반환.
    stock_only=True 시 ETF/ETN/우선주 제외.
    """
    global LAST_MKTCAP_INCOMPLETE
    LAST_MKTCAP_INCOMPLETE = False
    result: dict[str, float] = {}
    for m in markets:
        for page in range(1, max_pages + 1):
            url = f"{_MKTCAP_BASE}?type={m}&page={page}" if page > 1 else f"{_MKTCAP_BASE}?type={m}"
            html = _get(url, f"시총 {m} p{page}")
            if html is None:
                # 빈 페이지(자연 종료)와 달리 네트워크 실패는 '남은 페이지 유실'이다.
                LAST_MKTCAP_INCOMPLETE = True
                break
            rows = _parse_mktcap_page(html)
            if not rows:
                break
            for code, name, cap in rows:
                if stock_only and not _is_common_stock(code, name):
                    continue
                if cap > 0:
                    result[code] = cap
    return result


if __name__ == "__main__":
    df = fetch_ranking(top_n=100, stock_only=True)
    print(f"거래대금 수집 {len(df)}종목")
    print(df.head(20)[["code", "name", "price", "change_pct", "value_won", "market"]]
          .to_string(index=False))
    import sys
    if "--mktcap" in sys.argv:
        m = fetch_marketcap_map(max_pages=3)
        print(f"\n시총 수집 {len(m)}종목")
        top = sorted(m.items(), key=lambda x: x[1], reverse=True)[:10]
        for c, v in top:
            print(f"  {c} {v/1e8:>12,.0f}억")
