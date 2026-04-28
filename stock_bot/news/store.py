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


def _title_key(title: str) -> str:
    """대괄호 태그·공백 제거 후 앞 30자 — 제목 중복 판정 키."""
    import re as _re
    t = _re.sub(r"^\[[^\]]+\]\s*", "", title or "")
    t = _re.sub(r"[\s·ㆍ]+", "", t)
    return t[:30]


def news_exists(symbol: str, url: str) -> bool:
    """이미 저장된 기사면 True (LLM 호출 전 중복 체크용)."""
    with Session(NEWS_ENGINE) as s:
        return s.scalar(
            select(NewsRow.id).where(
                NewsRow.symbol == symbol, NewsRow.url == url
            ).limit(1)
        ) is not None


def news_title_exists(symbol: str, title: str, hours: int = 24) -> bool:
    """같은 제목(앞 30자 기준)의 기사가 최근 hours 시간 내 있으면 True."""
    key = _title_key(title)
    if not key:
        return False
    since = datetime.utcnow() - timedelta(hours=hours)
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow.title)
            .where(NewsRow.symbol == symbol)
            .where(NewsRow.published_at >= since)
        ).all()
        return any(_title_key(t) == key for t in rows)


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


def recent_sentiment(symbol: str, hours: int = 24) -> tuple[float, int, int]:
    """최근 뉴스 가중 감성 점수, 총 기사 수, critical 기사 수.

    hours 인수는 하위 호환용으로 유지하되,
    실전 봇은 news_since_kst() 로 동적 창을 사용한다.
    Returns (weighted_avg_score, article_count, critical_count).
    """
    since = datetime.utcnow() - timedelta(hours=hours)
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow)
            .where(NewsRow.symbol == symbol)
            .where(NewsRow.published_at >= since)
        ).all()
        if not rows:
            return 0.0, 0, 0
        total_w = sum(max(r.weight, 0.01) for r in rows)
        weighted = sum(r.sentiment_score * max(r.weight, 0.01) for r in rows)
        avg = weighted / total_w if total_w > 0 else 0.0
        crit = sum(1 for r in rows if r.is_critical)
        return avg, len(rows), crit


def recent_news_articles(
    symbol: str,
    before: datetime,
    hours: int = 24,
    limit: int = 5,
) -> list[dict]:
    """매매 시점(before) 기준 최근 기사 목록 반환."""
    since = before - timedelta(hours=hours)
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow)
            .where(NewsRow.symbol == symbol)
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


def recent_sentiment_dynamic(symbol: str) -> tuple[float, int, int]:
    """시간대별 동적 창으로 감성 점수 반환.

    news_since_kst() 가 결정한 since 이후 기사만 집계.
    """
    since = news_since_kst()
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow)
            .where(NewsRow.symbol == symbol)
            .where(NewsRow.published_at >= since)
        ).all()
        if not rows:
            return 0.0, 0, 0
        total_w = sum(max(r.weight, 0.01) for r in rows)
        weighted = sum(r.sentiment_score * max(r.weight, 0.01) for r in rows)
        avg = weighted / total_w if total_w > 0 else 0.0
        crit = sum(1 for r in rows if r.is_critical)
        return avg, len(rows), crit
