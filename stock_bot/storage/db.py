"""SQLite 거래 로그 저장."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

ENGINE = create_engine("sqlite:///trades.db", future=True)


class Base(DeclarativeBase):
    pass


class TradeLog(Base):
    __tablename__ = "trade_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(128), default="")
    broker_response: Mapped[str] = mapped_column(String(512), default="")


def init_db() -> None:
    Base.metadata.create_all(ENGINE)


def record_trade(
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    reason: str = "",
    broker_response: str = "",
) -> int:
    with Session(ENGINE) as session:
        trade = TradeLog(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            reason=reason,
            broker_response=broker_response[:512],
        )
        session.add(trade)
        session.commit()
        return trade.id
