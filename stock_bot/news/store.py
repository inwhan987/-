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


def save_news(
    item: NewsItem,
    score: float,
    method: str,
    weight: float = 1.0,
    is_critical: bool = False,
) -> bool:
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


def recent_sentiment(symbol: str, hours: int = 24) -> tuple[float, int, int]:
    """최근 N시간 가중 감성 점수, 총 기사 수, critical 기사 수.

    Returns (weighted_avg_score, article_count, critical_count).
    가중 평균: Σ(score × weight) / Σ(weight).
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
