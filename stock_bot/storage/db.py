"""SQLite 거래 로그 + 장마감 리뷰 저장."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from stock_bot.market_calendar import utcnow as _utcnow

ENGINE = create_engine("sqlite:///trades.db", future=True)


class Base(DeclarativeBase):
    pass


class TradeLog(Base):
    __tablename__ = "trade_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(256), default="")
    broker_response: Mapped[str] = mapped_column(String(512), default="")
    strategy: Mapped[str] = mapped_column(String(32), default="")
    details: Mapped[str] = mapped_column(Text, default="")   # JSON


class ReviewLog(Base):
    """장마감 후 Claude 가 그날의 거래를 리뷰한 결과."""
    __tablename__ = "review_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    date: Mapped[str] = mapped_column(String(16), index=True)  # YYYY-MM-DD
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    findings: Mapped[str] = mapped_column(Text, default="")    # JSON: list of observations
    suggestions: Mapped[str] = mapped_column(Text, default="")  # JSON: proposed adjustments
    raw_context: Mapped[str] = mapped_column(Text, default="")  # 전달한 context 원문
    # 2026-08-24: 리뷰 주체 구분. "ensemble"(스톡봇) / "leader"(대장주봇).
    # 두 전략은 유니버스·지표·파라미터가 분리돼 있어 한 리뷰로 묶으면
    # 매일 한쪽이 표본 0 이 되고 제안이 서로 오염된다 → 분리 저장.
    kind: Mapped[str] = mapped_column(String(16), default="ensemble", index=True)


def _migrate() -> None:
    """trade_log 에 strategy/details 컬럼 없으면 추가."""
    with ENGINE.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(trade_log)").fetchall()}
        if "strategy" not in cols:
            conn.exec_driver_sql("ALTER TABLE trade_log ADD COLUMN strategy VARCHAR(32) DEFAULT ''")
        if "details" not in cols:
            conn.exec_driver_sql("ALTER TABLE trade_log ADD COLUMN details TEXT DEFAULT ''")
        rcols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(review_log)").fetchall()}
        if rcols and "kind" not in rcols:
            # 기존 행은 전부 앙상블 리뷰였다 → 기본값 ensemble 로 채운다.
            conn.exec_driver_sql(
                "ALTER TABLE review_log ADD COLUMN kind VARCHAR(16) DEFAULT 'ensemble'")
            conn.exec_driver_sql("UPDATE review_log SET kind='ensemble' WHERE kind IS NULL")


def init_db() -> None:
    Base.metadata.create_all(ENGINE)
    _migrate()


def record_trade(
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    reason: str = "",
    broker_response: str = "",
    strategy: str = "",
    details: dict[str, Any] | None = None,
) -> int:
    # 종목코드 정규화: yfinance식 접미사(.KS/.KQ)가 새어들면 FIFO 실현손익이
    # 같은 종목을 별개로 취급해 매도-매수 매칭이 깨진다(005930 vs 005930.KS).
    # 모든 호출자가 거치는 단일 기록 지점에서 6자리 코드로 통일한다.
    symbol = symbol.split(".")[0]
    with Session(ENGINE) as session:
        trade = TradeLog(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            reason=reason[:256],
            broker_response=broker_response[:512],
            strategy=strategy,
            details=json.dumps(details or {}, ensure_ascii=False),
        )
        session.add(trade)
        session.commit()
        return trade.id


def record_review(
    date: str,
    trades_count: int,
    summary: str,
    findings: list[dict] | list[str],
    suggestions: list[dict] | list[str],
    raw_context: str = "",
    kind: str = "ensemble",
) -> int:
    with Session(ENGINE) as session:
        review = ReviewLog(
            date=date,
            trades_count=trades_count,
            summary=summary,
            findings=json.dumps(findings, ensure_ascii=False),
            suggestions=json.dumps(suggestions, ensure_ascii=False),
            raw_context=raw_context,
            kind=kind,
        )
        session.add(review)
        session.commit()
        return review.id
