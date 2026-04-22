"""뉴스 백필 크롤러.

기본 `news.db` 와 분리된 별도 SQLite(`news_backfill.db`) 에
지정한 기간(기본 30일)치 네이버 금융 뉴스를 모아 둔다.
나중에 `impact.py` 가 이 DB 를 읽어 키워드-주가 영향을 분석한다.

사용:
    python -m stock_bot.news.backfill --days 30 --symbols 005930,000660
    python -m stock_bot.news.backfill --days 30   # settings.symbols 사용
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

# score_sentiment_llm 이 os.environ 을 보므로 import 전에 .env 주입
# override=True: 부모 프로세스가 빈 값으로 주입해둔 경우(Claude Code 등)를 덮어씀
load_dotenv(override=True)
from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stock_bot.config import settings
from stock_bot.names import get_name
from stock_bot.news.crawler import NewsItem, USER_AGENT, fetch_naver_news
from stock_bot.news.naver_api import search_news as naver_api_search
from stock_bot.news.sentiment import score_sentiment, score_sentiment_keyword
from stock_bot.news.store import Base, NewsRow

DEFAULT_DB_PATH = "news_backfill.db"

# 다중쿼리 기본 주제 키워드. 회사명과 조합해 검색.
MULTI_QUERY_TOPICS = [
    "", "실적", "공시", "급등", "하락", "전망", "목표가",
    "신제품", "수주", "계약", "인수", "합병", "배당", "자사주",
    "AI", "반도체", "메모리", "파운드리", "HBM", "수출",
    "호재", "악재", "리스크", "규제", "소송",
]


def make_engine(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    return engine


def backfill_symbol(
    symbol: str,
    engine,
    days: int,
    max_pages: int,
    page_delay: float,
    client: httpx.Client,
) -> tuple[int, int]:
    """한 종목을 cutoff 이전 기사가 나올 때까지 페이지네이션.

    Returns: (saved, seen)
    """
    cutoff = datetime.now() - timedelta(days=days)
    saved = seen = 0
    stagnant_pages = 0
    min_oldest: datetime | None = None

    for page in range(1, max_pages + 1):
        # fetch_naver_news 는 내부 for-loop 이 있지만 pages=1 로 호출해 페이지 단위 제어.
        # page 인자를 직접 넘길 수 있도록 하위 호출을 재사용한다.
        items = fetch_naver_news(symbol, pages=1, client=client) if page == 1 else _fetch_page(
            symbol, page, client
        )
        if not items:
            logger.info("[{}] page {} 비어있음, 중단", symbol, page)
            break

        page_oldest = min(it.published_at for it in items)
        for it in items:
            seen += 1
            if it.published_at < cutoff:
                continue
            score = score_sentiment_keyword(it.title + " " + it.summary).score
            with Session(engine) as s:
                row = NewsRow(
                    symbol=it.symbol,
                    title=it.title,
                    url=it.url,
                    publisher=it.publisher,
                    published_at=it.published_at,
                    sentiment_score=score,
                    sentiment_method="keyword",
                    summary=it.summary,
                )
                s.add(row)
                try:
                    s.commit()
                    saved += 1
                except IntegrityError:
                    s.rollback()

        logger.info(
            "[{}] page {:3d}  page_oldest={}  saved_total={}",
            symbol,
            page,
            page_oldest.strftime("%Y-%m-%d %H:%M"),
            saved,
        )

        # cutoff 이전 기사가 섞여 나오기 시작하면 종료 (경계 한 페이지는 처리됨).
        if page_oldest < cutoff:
            logger.info("[{}] cutoff 도달, 페이지 {} 에서 종료", symbol, page)
            break
        # 지금까지 본 최저 시각을 갱신 못 하는 페이지가 연속 3번이면 중단.
        if min_oldest is None or page_oldest < min_oldest:
            min_oldest = page_oldest
            stagnant_pages = 0
        else:
            stagnant_pages += 1
            if stagnant_pages >= 3:
                logger.info("[{}] 페이지 진척 없음(3회 연속), 종료", symbol)
                break
        time.sleep(page_delay)

    return saved, seen


def _fetch_page(symbol: str, page: int, client: httpx.Client):
    """fetch_naver_news 의 단일 페이지 버전 (내부용)."""
    from stock_bot.news.crawler import BASE_URL, parse_news_html

    referer = f"https://finance.naver.com/item/main.naver?code={symbol}"
    r = client.get(
        BASE_URL,
        params={"code": symbol, "page": page},
        headers={"Referer": referer},
    )
    r.raise_for_status()
    html = r.content.decode("euc-kr", errors="replace")
    return parse_news_html(html, symbol)


def _score_items_llm(
    items: list[NewsItem], max_workers: int = 4, rpm: int = 45
) -> dict[str, tuple[float, str]]:
    """LLM 으로 각 기사 감성을 병렬 채점. 실패 시 keyword 로 fallback.

    rpm: 분당 최대 요청 수 (Anthropic Tier 1 Haiku 기본 50 → 45 로 여유).
    """
    import threading
    scores: dict[str, tuple[float, str]] = {}
    min_interval = 60.0 / max(1, rpm)
    lock = threading.Lock()
    last = [0.0]

    def rate_limit() -> None:
        with lock:
            now = time.time()
            wait = last[0] + min_interval - now
            if wait > 0:
                time.sleep(wait)
            last[0] = time.time()

    def worker(it: NewsItem) -> tuple[str, float, str]:
        rate_limit()
        text = (it.title + " " + it.summary).strip()
        res = score_sentiment(text, prefer_llm=True)
        return it.url, res.score, res.method

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(worker, it): it for it in items}
        for fut in as_completed(futures):
            try:
                url, score, method = fut.result()
                scores[url] = (score, method)
            except Exception as exc:
                it = futures[fut]
                logger.warning("LLM 실패 {}: {}", it.url[:60], exc)
                scores[it.url] = (
                    score_sentiment_keyword(it.title + " " + it.summary).score,
                    "keyword",
                )
            done += 1
            if done % 100 == 0:
                logger.info("  LLM 채점 {}/{}", done, len(items))
    return scores


def _save_items(
    items: list[NewsItem],
    engine,
    scores: dict[str, tuple[float, str]] | None = None,
) -> int:
    """scores 가 주어지면 그걸 쓰고, 없으면 keyword 로 즉석 채점."""
    saved = 0
    for it in items:
        if scores is not None and it.url in scores:
            score, method = scores[it.url]
        else:
            score = score_sentiment_keyword(it.title + " " + it.summary).score
            method = "keyword"
        with Session(engine) as s:
            row = NewsRow(
                symbol=it.symbol,
                title=it.title,
                url=it.url,
                publisher=it.publisher,
                published_at=it.published_at,
                sentiment_score=score,
                sentiment_method=method,
                summary=it.summary,
            )
            s.add(row)
            try:
                s.commit()
                saved += 1
            except IntegrityError:
                s.rollback()
    return saved


def bucket_hourly(items: list[NewsItem], per_bucket: int = 1) -> list[NewsItem]:
    """같은 (연,월,일,시) 버킷당 per_bucket 건만 남김 (가장 이른 시각)."""
    items_sorted = sorted(items, key=lambda i: i.published_at)
    buckets: dict[tuple[int, int, int, int], list[NewsItem]] = {}
    for it in items_sorted:
        key = (
            it.published_at.year,
            it.published_at.month,
            it.published_at.day,
            it.published_at.hour,
        )
        buckets.setdefault(key, [])
        if len(buckets[key]) < per_bucket:
            buckets[key].append(it)
    out: list[NewsItem] = []
    for vs in buckets.values():
        out.extend(vs)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="뉴스 한 달치 백필")
    ap.add_argument("--days", type=int, default=30, help="며칠 전까지 긁을지 (기본 30)")
    ap.add_argument(
        "--symbols",
        type=str,
        default="",
        help="쉼표구분 종목코드. 비우면 settings.symbols 사용",
    )
    ap.add_argument(
        "--source",
        choices=["naver_item", "naver_api"],
        default="naver_api",
        help="뉴스 소스: naver_item=종목페이지 크롤(최근 ~5일), naver_api=개발자 검색 API(최대 30일)",
    )
    ap.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="저장할 SQLite 경로")
    ap.add_argument("--max-pages", type=int, default=200, help="[item] 종목당 최대 페이지")
    ap.add_argument("--page-delay", type=float, default=0.7, help="[item] 페이지 간 대기초")
    ap.add_argument("--symbol-delay", type=float, default=2.0, help="종목 간 대기초")
    ap.add_argument(
        "--query",
        type=str,
        default="",
        help="[api] 검색어. 비우면 회사명 사용",
    )
    ap.add_argument(
        "--hourly-bucket",
        type=int,
        default=0,
        help="[api] >0 이면 (일,시) 버킷당 N건만 저장 (예: 1 이면 시간당 1건)",
    )
    ap.add_argument(
        "--max-items", type=int, default=1000, help="[api] 쿼리당 최대 수집 건수(≤1000)"
    )
    ap.add_argument(
        "--multi-query",
        action="store_true",
        help="[api] 회사명 + 주제 키워드 조합으로 여러 번 검색해 과거까지 확장",
    )
    ap.add_argument(
        "--query-delay",
        type=float,
        default=0.3,
        help="[api] 다중쿼리 간 대기초",
    )
    ap.add_argument(
        "--sort",
        choices=["sim", "date"],
        default="sim",
        help="[api] sim=유사도(과거까지 섞임), date=최신순",
    )
    ap.add_argument(
        "--llm",
        action="store_true",
        help="감성 점수를 Claude (Haiku) 로 채점 (ANTHROPIC_API_KEY 필요)",
    )
    ap.add_argument(
        "--llm-workers",
        type=int,
        default=4,
        help="LLM 병렬 채점 스레드 수",
    )
    ap.add_argument(
        "--llm-rpm",
        type=int,
        default=45,
        help="LLM 분당 최대 요청 수 (Anthropic Tier 1 Haiku = 50, 기본 45)",
    )
    args = ap.parse_args()

    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else settings.symbols
    )
    if not symbols:
        raise SystemExit("종목이 없습니다. --symbols 또는 TRADE_SYMBOLS 확인")

    db_path = str(Path(args.db).resolve())
    engine = make_engine(args.db)
    logger.info("백필 시작: symbols={} days={} db={}", symbols, args.days, db_path)

    totals: dict[str, tuple[int, int]] = {}
    if args.source == "naver_item":
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0) as client:
            for i, sym in enumerate(symbols):
                saved, seen = backfill_symbol(
                    sym, engine, args.days, args.max_pages, args.page_delay, client
                )
                totals[sym] = (saved, seen)
                logger.info("[{}] 완료: saved={} seen={}", sym, saved, seen)
                if i < len(symbols) - 1:
                    time.sleep(args.symbol_delay)
    else:  # naver_api
        with httpx.Client(timeout=15.0) as client:
            for i, sym in enumerate(symbols):
                base_query = args.query or get_name(sym) or sym
                if args.multi_query:
                    queries = [
                        f"{base_query} {t}".strip() for t in MULTI_QUERY_TOPICS
                    ]
                else:
                    queries = [base_query]
                logger.info(
                    "[{}] api queries={} days={}", sym, len(queries), args.days
                )

                pool: dict[str, NewsItem] = {}  # url → item 중복제거
                api_total = 0
                for qi, q in enumerate(queries):
                    result = naver_api_search(
                        query=q,
                        symbol=sym,
                        days=args.days,
                        max_items=args.max_items,
                        sort=args.sort,
                        client=client,
                    )
                    api_total = max(api_total, result.total)
                    before = len(pool)
                    for it in result.items:
                        pool.setdefault(it.url, it)
                    logger.info(
                        "[{}] q{:02d} '{}' → +{} (누적 {})",
                        sym,
                        qi + 1,
                        q,
                        len(pool) - before,
                        len(pool),
                    )
                    if qi < len(queries) - 1:
                        time.sleep(args.query_delay)

                items = list(pool.values())
                seen = len(items)
                if args.hourly_bucket > 0:
                    items = bucket_hourly(items, per_bucket=args.hourly_bucket)
                    logger.info(
                        "[{}] 시간 버킷팅: {} → {} (버킷당 {}건)",
                        sym,
                        seen,
                        len(items),
                        args.hourly_bucket,
                    )
                scores = None
                if args.llm:
                    logger.info(
                        "[{}] LLM 채점 시작: {}건 × {}워커",
                        sym,
                        len(items),
                        args.llm_workers,
                    )
                    t_llm = time.time()
                    scores = _score_items_llm(
                        items, max_workers=args.llm_workers, rpm=args.llm_rpm
                    )
                    logger.info(
                        "[{}] LLM 채점 완료: {}건 in {:.1f}s",
                        sym,
                        len(scores),
                        time.time() - t_llm,
                    )
                saved = _save_items(items, engine, scores=scores)
                totals[sym] = (saved, seen)
                logger.info(
                    "[{}] 완료: saved={} seen={} (api 보고 total≤{})",
                    sym,
                    saved,
                    seen,
                    api_total,
                )
                if i < len(symbols) - 1:
                    time.sleep(args.symbol_delay)

    logger.info("=== 백필 결과 ===")
    for sym, (saved, seen) in totals.items():
        logger.info("{}  saved={:5d}  seen={:5d}", sym, saved, seen)


if __name__ == "__main__":
    main()
