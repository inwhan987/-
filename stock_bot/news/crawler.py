"""네이버 금융 종목 뉴스 크롤러.

대상 URL: https://finance.naver.com/item/news_news.naver?code={SYMBOL}&page=1

table.type5 아래 각 기사 행에서 제목/URL/언론사/날짜/요약을 추출한다.
robots/ToS 를 고려해 요청 간격을 둘 것.
"""
from __future__ import annotations

import re
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


def fetch_naver_news(
    symbol: str, pages: int = 1, delay_sec: float = 0.5,
    client: httpx.Client | None = None,
    since: datetime | None = None,
) -> list[NewsItem]:
    """주어진 종목코드의 네이버 금융 뉴스 목록.

    since 가 주어지면 published_at < since 인 기사를 만나는 즉시 중단 (early stop).
    네이버 뉴스는 최신순 정렬이므로 이후 페이지는 모두 오래된 기사.
    """
    owns_client = client is None
    cli = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10.0)
    code = symbol.split(".")[0]  # 005930.KS → 005930 (네이버는 6자리 코드만 인식)
    collected: list[NewsItem] = []
    try:
        for page in range(1, pages + 1):
            referer = f"https://finance.naver.com/item/main.naver?code={code}"
            # 일시적 DNS/네트워크 이슈 대비 3회 재시도 (1.5초 간격)
            r = None
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    r = cli.get(
                        BASE_URL,
                        params={"code": code, "page": page},
                        headers={"Referer": referer},
                    )
                    r.raise_for_status()
                    if attempt > 1:
                        logger.info("naver news recovered on attempt {}/3 for {}", attempt, symbol)
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.debug("naver news fetch attempt {}/3 failed for {}: {}", attempt, symbol, exc)
                    if attempt < 3:
                        time.sleep(1.5)
            if r is None:
                # 3회 모두 실패 → 이번 페이지 스킵하고 상위 호출자에게 예외 전파
                raise last_exc if last_exc else Exception("naver news fetch failed")
            # 네이버는 EUC-KR 기본이지만 meta 태그로 감지됨. 원문에서 직접 디코드.
            html = r.content.decode("euc-kr", errors="replace")
            items = parse_news_html(html, code)  # DB에 6자리 코드로 저장
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
