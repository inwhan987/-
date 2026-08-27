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


# 일회성 데이터 정정. (조건, SQL, 설명) — 조건이 0행이면 아무것도 하지 않는다.
# 파이에 SSH 가 없어 DB 를 직접 못 고치므로, 배포로 흘러가는 코드에 넣어
# 기동 시 한 번 자기 자신을 정정하게 한다. 조건에 원본 값을 전부 박아두어
# 두 번 실행돼도, 이미 고쳐진 DB 에서도 매칭이 안 되게 했다(멱등).
_FIXUPS: list[tuple[str, str, str]] = [
    (
        # 2026-08-27 HD현대일렉트릭(267260): 시장가 62주 주문이 1호가 잔량에
        # 막혀 실제로는 31주만 체결됐는데 주문 수량 62 가 그대로 기록됐다.
        # (당시엔 체결수량 확인이 없었다 — 이후 get_order_fill 로 보정한다.)
        # 매도는 분할익절 31주 한 건뿐이라 이대로 두면 267260 을 영구히 31주
        # 보유 중인 것으로 계산해 FIFO 실현손익·보유수량이 계속 틀어진다.
        "SELECT COUNT(*) FROM trade_log WHERE id=23 AND symbol='267260' "
        "AND side='buy' AND quantity=62 AND price=794000.0",
        "UPDATE trade_log SET quantity=31 WHERE id=23 AND symbol='267260' "
        "AND side='buy' AND quantity=62 AND price=794000.0",
        "267260 매수 수량 62 → 실제 체결 31 정정",
    ),
]


def _fixups() -> None:
    with ENGINE.begin() as conn:
        for check, sql, desc in _FIXUPS:
            try:
                if conn.exec_driver_sql(check).scalar() or 0:
                    conn.exec_driver_sql(sql)
                    print(f"[db] fixup applied: {desc}")
            except Exception as exc:  # 정정 실패가 기동을 막으면 안 된다
                print(f"[db] fixup skipped ({desc}): {exc}")


def init_db() -> None:
    Base.metadata.create_all(ENGINE)
    _migrate()
    _fixups()


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
