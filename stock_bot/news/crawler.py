"""네이버 금융 종목 뉴스 크롤러.

2026-09-20: finance.naver.com/item/news_news.naver 가 410 Gone (PC 금융 → stock.naver.com 이전).
새 사이트가 쓰는 모바일 JSON API(API_URL) 를 1차로 쓰고, 구 HTML 은 폴백으로만 남긴다.
API 응답: [ {"total": n, "items": [ {officeId, articleId, officeName, datetime "YYYYMMDDHHMM",
title, body, mobileNewsUrl}, ... ]}, ... ]  (같은 사건 기사끼리 묶인 그룹 리스트)

구 URL: https://finance.naver.com/item/news_news.naver?code={SYMBOL}&page=1
table.type5 아래 각 기사 행에서 제목/URL/언론사/날짜/요약을 추출한다.
robots/ToS 를 고려해 요청 간격을 둘 것.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import httpx
from bs4 import BeautifulSoup
from loguru import logger

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BASE_URL = "https://finance.naver.com/item/news_news.naver"
API_URL = "https://m.stock.naver.com/api/news/stock/{code}"
API_PAGE_SIZE = 20   # 구 HTML 한 페이지(20건)와 맞춤


@dataclass
class NewsItem:
    symbol: str
    title: str
    url: str
    publisher: str
    published_at: datetime
    summary: str = ""

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.url}"


def _parse_row(row, symbol: str) -> NewsItem | None:
    title_td = row.select_one("td.title a")
    info_td = row.select_one("td.info")
    date_td = row.select_one("td.date")
    if not (title_td and date_td):
        return None
    title = title_td.get_text(strip=True)
    url = title_td.get("href", "")
    if url.startswith("/"):
        url = "https://finance.naver.com" + url
    publisher = info_td.get_text(strip=True) if info_td else ""
    date_text = date_td.get_text(strip=True)
    try:
        published_at = datetime.strptime(date_text, "%Y.%m.%d %H:%M")
    except ValueError:
        return None
    return NewsItem(
        symbol=symbol,
        title=title,
        url=url,
        publisher=publisher,
        published_at=published_at,
    )


def parse_news_api(data, symbol: str) -> list[NewsItem]:
    """모바일 API JSON → NewsItem 목록 (그룹 평탄화, 최신순 유지)."""
    items: list[NewsItem] = []
    for group in data or []:
        for it in (group.get("items") or []):
            try:
                published_at = datetime.strptime(str(it.get("datetime", "")), "%Y%m%d%H%M")
            except ValueError:
                continue
            url = it.get("mobileNewsUrl") or ""
            if not url:
                oid, aid = it.get("officeId"), it.get("articleId")
                if not (oid and aid):
                    continue
                url = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
            items.append(NewsItem(
                symbol=symbol,
                title=(it.get("titleFull") or it.get("title") or "").strip(),
                url=url,
                publisher=(it.get("officeName") or "").strip(),
                published_at=published_at,
                summary=(it.get("body") or "").strip(),
            ))
    return items


def fetch_page(code: str, page: int, cli: httpx.Client) -> list[NewsItem]:
    """단일 페이지 — 모바일 API 1차, 실패 시 구 HTML 폴백. 예외는 호출자에게 전파."""
    referer = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        r = cli.get(
            API_URL.format(code=code),
            params={"pageSize": API_PAGE_SIZE, "page": page},
            headers={"Referer": f"https://m.stock.naver.com/domestic/stock/{code}/news"},
        )
        r.raise_for_status()
        return parse_news_api(r.json(), code)
    except Exception as exc:
        logger.debug("naver news api failed for {} p{}: {} → HTML 폴백", code, page, exc)
    r = cli.get(BASE_URL, params={"code": code, "page": page}, headers={"Referer": referer})
    r.raise_for_status()
    html = r.content.decode("euc-kr", errors="replace")
    return parse_news_html(html, code)


def parse_news_html(html: str, symbol: str) -> list[NewsItem]:
    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    # 네이버 뉴스 테이블: class='type5'
    for row in soup.select("table.type5 tr"):
        # 관련뉴스 접힘 행 skip
        classes = row.get("class", [])
        if "relation_tit" in classes:
            continue
        item = _parse_row(row, symbol)
        if item:
            items.append(item)
    return items


def _is_relevant(item: NewsItem, code: str, company_name: str) -> bool:
    """제목에 종목코드 또는 회사명이 포함되는지 확인 (관련성 필터)."""
    text = (item.title + " " + item.summary).lower()
    if code.lower() in text:
        return True
    if company_name and len(company_name) >= 2:
        # 회사명 앞 2글자 이상 매칭 (예: '삼성전자' → '삼성' 포함이면 통과)
        if company_name.lower() in text:
            return True
        # 핵심 키워드: 첫 2글자만으로도 통과 (예: '삼성', 'SK', 'LG')
        if company_name[:2].lower() in text:
            return True
    return False


def fetch_naver_news(
    symbol: str, pages: int = 1, delay_sec: float = 0.5,
    client: httpx.Client | None = None,
    since: datetime | None = None,
    relevance_filter: bool = False,
) -> list[NewsItem]:
    """주어진 종목코드의 네이버 금융 뉴스 목록.

    since 가 주어지면 published_at < since 인 기사를 만나는 즉시 중단 (early stop).
    네이버 뉴스는 최신순 정렬이므로 이후 페이지는 모두 오래된 기사.
    """
    owns_client = client is None
    cli = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10.0)
    code = symbol.split(".")[0]  # 005930.KS → 005930 (네이버는 6자리 코드만 인식)

    # 관련성 필터용 회사명 조회
    _company_name = ""
    if relevance_filter:
        try:
            from stock_bot.names import get_name
            _company_name = get_name(symbol) or ""
        except Exception:
            pass

    collected: list[NewsItem] = []
    try:
        for page in range(1, pages + 1):
            # 일시적 DNS/네트워크 이슈 대비 3회 재시도 (1.5초 간격)
            items = None
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    items = fetch_page(code, page, cli)  # DB에 6자리 코드로 저장
                    if attempt > 1:
                        logger.info("naver news recovered on attempt {}/3 for {}", attempt, symbol)
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.debug("naver news fetch attempt {}/3 failed for {}: {}", attempt, symbol, exc)
                    if attempt < 3:
                        time.sleep(1.5)
            if items is None:
                # 3회 모두 실패 → 상위 호출자에게 예외 전파
                raise last_exc if last_exc else Exception("naver news fetch failed")
            stop = False
            for item in items:
                if since and item.published_at < since:
                    stop = True
                    break
                collected.append(item)
            if stop:
                break
            if page < pages:
                time.sleep(delay_sec)

        # 관련성 필터: 제목+요약에 종목코드/회사명 없는 기사 제외
        if relevance_filter:
            before = len(collected)
            collected = [it for it in collected if _is_relevant(it, code, _company_name)]
            filtered = before - len(collected)
            if filtered:
                logger.info(
                    "news relevance filter: {} → {} (제외 {}건) [{}]",
                    before, len(collected), filtered, symbol,
                )

        logger.info("fetched {} news items for {}", len(collected), symbol)
        return collected
    finally:
        if owns_client:
            cli.close()


def dedupe(items: Iterable[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    out: list[NewsItem] = []
    for it in items:
        if it.key in seen:
            continue
        seen.add(it.key)
        out.append(it)
    return out
