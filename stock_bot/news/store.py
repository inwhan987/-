"""뉴스 + 감성 점수 SQLite 저장."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
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


def init_news_db() -> None:
    Base.metadata.create_all(NEWS_ENGINE)


def save_news(item: NewsItem, score: float, method: str) -> bool:
    """새 기사면 저장하고 True, 이미 있으면 False."""
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
        )
        s.add(row)
        try:
            s.commit()
            return True
        except IntegrityError:
            s.rollback()
            return False


def recent_sentiment(symbol: str, hours: int = 24) -> tuple[float, int]:
    """최근 N시간 동안의 평균 감성 점수와 기사 수. (score, count)."""
    since = datetime.utcnow() - timedelta(hours=hours)
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow)
            .where(NewsRow.symbol == symbol)
            .where(NewsRow.published_at >= since)
        ).all()
        if not rows:
            return 0.0, 0
        avg = sum(r.sentiment_score for r in rows) / len(rows)
        return avg, len(rows)
