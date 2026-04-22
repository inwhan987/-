"""네이버 개발자 뉴스 검색 API 클라이언트.

문서: https://developers.naver.com/docs/serviceapi/search/news/news.md
- display: 1..100
- start:   1..1000 (즉 쿼리당 최대 1000건)
- sort:    sim | date
- pubDate: RFC1123 (예: "Tue, 22 Apr 2026 09:15:00 +0900")
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from stock_bot.config import settings
from stock_bot.news.crawler import NewsItem

API_URL = "https://openapi.naver.com/v1/search/news.json"
MAX_DISPLAY = 100
MAX_START = 1000  # 즉 한 쿼리로 최대 1000건


@dataclass
class NaverAPIResult:
    items: list[NewsItem]
    total: int  # API 가 보고한 total (참고용)


def _clean(html: str) -> str:
    """검색 결과의 <b>태그/HTML 엔티티 제거."""
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _parse_item(raw: dict, symbol: str) -> NewsItem | None:
    try:
        pub = parsedate_to_datetime(raw["pubDate"])
        # tz-aware → naive(로컬이 아닌 KST 그대로)로 변환해 DB와 호환
        if pub.tzinfo is not None:
            pub = pub.astimezone().replace(tzinfo=None)
    except Exception:
        return None
    title = _clean(raw.get("title", ""))
    summary = _clean(raw.get("description", ""))
    url = raw.get("originallink") or raw.get("link") or ""
    if not title or not url:
        return None
    # publisher 는 API 에서 직접 주지 않음 → URL 호스트로 추정
    m = re.search(r"https?://([^/]+)/", url)
    publisher = m.group(1) if m else ""
    return NewsItem(
        symbol=symbol,
        title=title,
        url=url,
        publisher=publisher,
        published_at=pub,
        summary=summary,
    )


def search_news(
    query: str,
    symbol: str,
    days: int = 30,
    max_items: int = MAX_START,
    sort: str = "sim",
    client: httpx.Client | None = None,
    request_delay: float = 0.1,
) -> NaverAPIResult:
    """쿼리 결과를 최신순으로 긁어 cutoff 이내만 돌려준다."""
    if not settings.naver_client_id or not settings.naver_client_secret:
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 .env 에 없습니다")
    cutoff = datetime.now() - timedelta(days=days)
    headers = {
        "X-Naver-Client-Id": settings.naver_client_id,
        "X-Naver-Client-Secret": settings.naver_client_secret,
    }
    owns_client = client is None
    cli = client or httpx.Client(timeout=15.0)
    collected: list[NewsItem] = []
    total_reported = 0
    try:
        start = 1
        while start <= MAX_START and len(collected) < max_items:
            display = min(MAX_DISPLAY, max_items - len(collected), MAX_START - start + 1)
            r = cli.get(
                API_URL,
                headers=headers,
                params={"query": query, "display": display, "start": start, "sort": sort},
            )
            if r.status_code == 400 and start > 1:
                # start+display 가 1000 을 넘으면 에러 → 정상 종료
                break
            r.raise_for_status()
            data = r.json()
            total_reported = data.get("total", total_reported)
            raw_items = data.get("items", [])
            if not raw_items:
                break
            stop = False
            for raw in raw_items:
                item = _parse_item(raw, symbol)
                if item is None:
                    continue
                if item.published_at < cutoff:
                    stop = True
                    continue
                collected.append(item)
            logger.info(
                "[{}] naver-api start={:4d} display={:3d} got={:3d} kept={:4d}",
                symbol,
                start,
                display,
                len(raw_items),
                len(collected),
            )
            if stop:
                break
            start += display
            time.sleep(request_delay)
    finally:
        if owns_client:
            cli.close()
    return NaverAPIResult(items=collected, total=total_reported)
