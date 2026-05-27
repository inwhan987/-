"""뉴스 + 감성 점수 SQLite 저장."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    text as sqltext,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from stock_bot.news.crawler import NewsItem

NEWS_ENGINE = create_engine("sqlite:///news.db", future=True)


def _to_code(symbol: str) -> str:
    """뉴스 DB 조회용 심볼 정규화 — 6자리 코드 (000660.KS → 000660)."""
    return symbol.split(".")[0] if "." in symbol else symbol


class Base(DeclarativeBase):
    pass


class NewsRow(Base):
    __tablename__ = "news"
    __table_args__ = (UniqueConstraint("symbol", "url", name="uq_symbol_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(512))
    publisher: Mapped[str] = mapped_column(String(64), default="")
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_method: Mapped[str] = mapped_column(String(16), default="keyword")
    summary: Mapped[str] = mapped_column(Text, default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    is_critical: Mapped[bool] = mapped_column(Boolean, default=False)


def _migrate() -> None:
    """기존 DB 에 weight/is_critical 컬럼 없으면 추가."""
    with NEWS_ENGINE.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(news)").fetchall()}
        if "weight" not in cols:
            conn.exec_driver_sql("ALTER TABLE news ADD COLUMN weight FLOAT DEFAULT 1.0")
        if "is_critical" not in cols:
            conn.exec_driver_sql("ALTER TABLE news ADD COLUMN is_critical BOOLEAN DEFAULT 0")


def init_news_db() -> None:
    Base.metadata.create_all(NEWS_ENGINE)
    _migrate()


def _normalize_title(title: str) -> str:
    """대괄호 태그·특수문자·공백 제거 후 소문자화 — 유사도 비교용."""
    import re as _re
    t = _re.sub(r"\[[^\]]+\]", "", title or "")   # 모든 [태그] 제거
    t = _re.sub(r"[^\w가-힣a-zA-Z0-9]", "", t)   # 특수문자·공백 제거
    return t.lower()


def _title_similarity(a: str, b: str) -> float:
    """바이그램 Jaccard 유사도 (0~1). 0.7 이상이면 사실상 동일 기사."""
    if not a or not b:
        return 0.0
    def bigrams(s: str) -> set[str]:
        return {s[i:i+2] for i in range(len(s) - 1)}
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


_SIMILARITY_THRESHOLD = 0.7

# 단시간 내 동일 주제 중복 체크용 불용어 (종목명·조사 등 변별력 없는 단어)
_TOPIC_STOP = {
    "삼성전자", "삼성", "기자", "뉴스", "속보", "단독", "종합",
    "한국", "국내", "이번", "지난", "올해", "내년", "이후", "이전",
    "관련", "발표", "계획", "예정", "진행", "실시", "통해", "위해",
    "가장", "이상", "이하", "최대", "최소", "최고", "최저", "오전", "오후",
}


def _extract_topic_keywords(title: str) -> set[str]:
    """제목에서 2자 이상 한글 단어 추출 (불용어 제외) — 주제 중복 판정용."""
    import re as _re
    t = _re.sub(r"\[[^\]]+\]", "", title or "")
    t = _re.sub(r"[^\w가-힣]", " ", t)
    return {w for w in t.split() if len(w) >= 2} - _TOPIC_STOP


def _topic_similarity(a: set[str], b: set[str]) -> float:
    """키워드 집합 Jaccard 유사도."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def get_latest_news_ts(symbol: str) -> datetime | None:
    """DB에 저장된 해당 종목의 가장 최신 기사 published_at 반환."""
    with Session(NEWS_ENGINE) as s:
        return s.scalar(
            select(NewsRow.published_at)
            .where(NewsRow.symbol == symbol)
            .order_by(NewsRow.published_at.desc())
            .limit(1)
        )


def news_exists(symbol: str, url: str) -> bool:
    """이미 저장된 기사면 True (LLM 호출 전 중복 체크용)."""
    with Session(NEWS_ENGINE) as s:
        return s.scalar(
            select(NewsRow.id).where(
                NewsRow.symbol == symbol, NewsRow.url == url
            ).limit(1)
        ) is not None


def news_title_exists(symbol: str, title: str, hours: int = 24) -> bool:
    """유사 제목 기사가 최근 hours 시간 내 있으면 True.

    1차: 바이그램 Jaccard 0.7 이상 (제목 표현 유사)
    2차: 2시간 내 키워드 집합 Jaccard 0.4 이상 (동일 주제 다른 표현)
    """
    norm = _normalize_title(title)
    if not norm:
        return False
    now = datetime.utcnow()
    since_24h = now - timedelta(hours=hours)
    since_2h  = now - timedelta(hours=2)

    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow.title)
            .where(NewsRow.symbol == symbol)
            .where(NewsRow.published_at >= since_24h)
        ).all()

    kw_new = _extract_topic_keywords(title)
    for t in rows:
        # 1차: 바이그램 유사도
        if _title_similarity(norm, _normalize_title(t)) >= _SIMILARITY_THRESHOLD:
            return True
    # 2차: 2시간 내 키워드 주제 중복
    with Session(NEWS_ENGINE) as s:
        recent_rows = s.scalars(
            select(NewsRow.title)
            .where(NewsRow.symbol == symbol)
            .where(NewsRow.published_at >= since_2h)
        ).all()
    def _is_dup(other_title: str) -> bool:
        kw_other = _extract_topic_keywords(other_title)
        shared = kw_new & kw_other
        # Jaccard ≥ 0.3 이거나 의미있는 키워드 2개 이상 겹치면 동일 주제
        return (
            _topic_similarity(kw_new, kw_other) >= 0.3
            or len(shared) >= 2
        )
    return any(_is_dup(t) for t in recent_rows)


def save_news(
    item: NewsItem,
    score: float,
    method: str,
    weight: float = 1.0,
    is_critical: bool = False,
) -> bool:
    """새 기사면 저장하고 True, 이미 있으면(URL·제목 중복) False."""
    if news_title_exists(item.symbol, item.title):
        return False
    with Session(NEWS_ENGINE) as s:
        row = NewsRow(
            symbol=item.symbol,
            title=item.title,
            url=item.url,
            publisher=item.publisher,
            published_at=item.published_at,
            sentiment_score=score,
            sentiment_method=method,
            summary=item.summary,
            weight=weight,
            is_critical=is_critical,
        )
        s.add(row)
        try:
            s.commit()
            return True
        except IntegrityError:
            s.rollback()
            return False


def news_since_kst() -> datetime:
    """현재 KST 기준 뉴스 감성 조회 시작 시각 (UTC naive) 반환.

    월~금 10:00~   : 당일 09:00 (장중 뉴스만)
    월요일 ~10:00  : 금요일 15:30 (주말 뉴스)
    화~금  ~10:00  : 전날  15:30 (오버나이트 뉴스)
    """
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    now = datetime.now(KST)
    wd = now.weekday()  # 0=월

    if now.hour >= 10:  # 10시 이후 — 요일 무관 당일 장중
        since = now.replace(hour=9, minute=0, second=0, microsecond=0)
    elif wd == 0:  # 월요일 09~10시 → 금요일(3일 전) 15:30
        since = (now - timedelta(days=3)).replace(
            hour=15, minute=30, second=0, microsecond=0
        )
    else:  # 화~금 09~10시 → 전날 15:30
        since = (now - timedelta(days=1)).replace(
            hour=15, minute=30, second=0, microsecond=0
        )

    # UTC naive 로 변환 (DB 저장 기준)
    return since.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def recent_sentiment(symbol: str, hours: int = 24, strong_neg_threshold: float = -0.6) -> tuple[float, int, int, int]:
    """최근 뉴스 가중 감성 점수, 총 기사 수, critical 기사 수, 강한 부정 기사 수.

    hours 인수는 하위 호환용으로 유지하되,
    실전 봇은 news_since_kst() 로 동적 창을 사용한다.
    Returns (weighted_avg_score, article_count, critical_count, strong_neg_count).
    strong_neg_count: sentiment_score <= strong_neg_threshold 인 기사 수.
    """
    code = _to_code(symbol)
    since = datetime.utcnow() - timedelta(hours=hours)
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow)
            .where(NewsRow.symbol == code)
            .where(NewsRow.published_at >= since)
        ).all()
        if not rows:
            return 0.0, 0, 0, 0
        total_w = sum(max(r.weight, 0.01) for r in rows)
        weighted = sum(r.sentiment_score * max(r.weight, 0.01) for r in rows)
        avg = weighted / total_w if total_w > 0 else 0.0
        crit = sum(1 for r in rows if r.is_critical)
        strong_neg = sum(1 for r in rows if r.sentiment_score <= strong_neg_threshold)
        return avg, len(rows), crit, strong_neg


def recent_news_articles(
    symbol: str,
    before: datetime,
    hours: int = 24,
    limit: int = 5,
) -> list[dict]:
    """매매 시점(before) 기준 최근 기사 목록 반환."""
    code = _to_code(symbol)
    since = before - timedelta(hours=hours)
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow)
            .where(NewsRow.symbol == code)
            .where(NewsRow.published_at >= since)
            .where(NewsRow.published_at <= before)
            .order_by(NewsRow.published_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "title": r.title,
                "url": r.url,
                "publisher": r.publisher,
                "score": r.sentiment_score,
                "is_critical": bool(r.is_critical),
                "published_at": r.published_at.strftime("%m/%d %H:%M"),
            }
            for r in rows
        ]


def recent_sentiment_dynamic(symbol: str, strong_neg_threshold: float = -0.6) -> tuple[float, int, int, int]:
    """시간대별 동적 창으로 감성 점수 반환.

    news_since_kst() 가 결정한 since 이후 기사만 집계.
    Returns (weighted_avg, article_count, critical_count, strong_neg_count).
    """
    code = _to_code(symbol)
    since = news_since_kst()
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow)
            .where(NewsRow.symbol == code)
            .where(NewsRow.published_at >= since)
        ).all()
        if not rows:
            return 0.0, 0, 0, 0
        total_w = sum(max(r.weight, 0.01) for r in rows)
        weighted = sum(r.sentiment_score * max(r.weight, 0.01) for r in rows)
        avg = weighted / total_w if total_w > 0 else 0.0
        crit = sum(1 for r in rows if r.is_critical)
        strong_neg = sum(1 for r in rows if r.sentiment_score <= strong_neg_threshold)
        return avg, len(rows), crit, strong_neg
