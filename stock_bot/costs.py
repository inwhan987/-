"""Claude API 비용 추적.

사용한 토큰을 SQLite에 기록하고 일별/월별 요약을 제공한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import Float, Integer, String, DateTime, create_engine, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from stock_bot.market_calendar import KST as _KST, utcnow as _utcnow

COSTS_ENGINE = create_engine("sqlite:///costs.db", future=True)

# 모델별 가격 ($/MTok)
_PRICE: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00,  5.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-opus-4-7":           (5.00, 25.00),
    "claude-opus-4-8":           (5.00, 25.00),
}
_KRW_PER_USD = 1_400
# 웹서치 서버툴 요금 ($10 / 1,000 requests)
_WEB_SEARCH_USD = 0.01


class Base(DeclarativeBase):
    pass


class CostLog(Base):
    __tablename__ = "api_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    source: Mapped[str] = mapped_column(String(32))   # news_sentiment | daily_review
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)


def init_costs_db() -> None:
    Base.metadata.create_all(COSTS_ENGINE)


def record_cost(
    source: str, model: str, input_tokens: int, output_tokens: int,
    web_search_requests: int = 0,
) -> float:
    """토큰 사용량 저장 후 비용(USD) 반환. web_search_requests는 서버툴 검색 횟수."""
    in_p, out_p = _PRICE.get(model, (3.00, 15.00))
    cost = (input_tokens * in_p + output_tokens * out_p) / 1_000_000
    cost += max(0, web_search_requests) * _WEB_SEARCH_USD
    with Session(COSTS_ENGINE) as s:
        s.add(CostLog(
            source=source, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=cost,
        ))
        s.commit()
    logger.debug("API 비용: ${:.5f} | {} | {} in={} out={}", cost, source, model, input_tokens, output_tokens)
    return cost


def _date_range_utc(date_str: str) -> tuple[datetime, datetime]:
    """KST 날짜 문자열 → UTC start/end (naive)."""
    kst_midnight = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_KST)
    start = kst_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def daily_summary(date_str: str | None = None) -> dict:
    """날짜(기본 오늘 KST) 기준 비용 요약 dict."""
    date_str = date_str or datetime.now(tz=_KST).strftime("%Y-%m-%d")
    start, end = _date_range_utc(date_str)

    with Session(COSTS_ENGINE) as s:
        rows = s.execute(
            select(
                CostLog.source,
                func.sum(CostLog.cost_usd),
                func.sum(CostLog.input_tokens),
                func.sum(CostLog.output_tokens),
                func.count(),
            )
            .where(CostLog.ts >= start, CostLog.ts < end)
            .group_by(CostLog.source)
        ).all()

    total_usd = sum(r[1] for r in rows)
    breakdown = {
        r[0]: {
            "cost_usd": round(r[1], 5),
            "cost_krw": int(r[1] * _KRW_PER_USD),
            "input_tokens": r[2],
            "output_tokens": r[3],
            "calls": r[4],
        }
        for r in rows
    }
    return {
        "date": date_str,
        "total_usd": round(total_usd, 5),
        "total_krw": int(total_usd * _KRW_PER_USD),
        "breakdown": breakdown,
    }


def monthly_summary(year_month: str | None = None) -> dict:
    """월별 비용 요약. year_month='2026-04' 형식."""
    now_kst = datetime.now(tz=_KST)
    ym = year_month or now_kst.strftime("%Y-%m")
    y, m = int(ym[:4]), int(ym[5:7])
    start_kst = datetime(y, m, 1, tzinfo=_KST)
    if m == 12:
        end_kst = datetime(y + 1, 1, 1, tzinfo=_KST)
    else:
        end_kst = datetime(y, m + 1, 1, tzinfo=_KST)
    start = start_kst.astimezone(timezone.utc).replace(tzinfo=None)
    end = end_kst.astimezone(timezone.utc).replace(tzinfo=None)

    with Session(COSTS_ENGINE) as s:
        total_usd = s.scalar(
            select(func.sum(CostLog.cost_usd))
            .where(CostLog.ts >= start, CostLog.ts < end)
        ) or 0.0

    return {
        "month": ym,
        "total_usd": round(total_usd, 4),
        "total_krw": int(total_usd * _KRW_PER_USD),
    }


def total_spent() -> float:
    """전체 누적 사용액 (USD)."""
    with Session(COSTS_ENGINE) as s:
        return s.scalar(select(func.sum(CostLog.cost_usd))) or 0.0


def spent_since(since_ts: float | None) -> float:
    """리셋 시점(epoch UTC) 이후 누적 사용액 (USD). since_ts=0/None이면 전체 기간."""
    if not since_ts:
        return total_spent()
    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc).replace(tzinfo=None)
    with Session(COSTS_ENGINE) as s:
        return s.scalar(
            select(func.sum(CostLog.cost_usd)).where(CostLog.ts >= since_dt)
        ) or 0.0


def format_daily_report(date_str: str | None = None) -> str:
    """Discord용 일일 비용 리포트 문자열."""
    from stock_bot.config import settings

    s = daily_summary(date_str)
    mo = monthly_summary()
    budget = settings.api_budget_usd
    # A안: 충전 시점(API_BUDGET_RESET_AT) 이후 사용액만 카운트. budget=이번 충전액.
    reset_at = getattr(settings, "api_budget_reset_at", 0.0) or 0.0
    spent_cycle = spent_since(reset_at)
    remaining = max(0.0, budget - spent_cycle) if budget > 0 else None

    lines = [f"💰 **API 비용 ({s['date']} KST)**"]
    lines.append(f"  어제: ${s['total_usd']:.4f} ({s['total_krw']:,}원)")
    for src, d in s["breakdown"].items():
        label = {
            "news_sentiment": "뉴스 감성분석",
            "daily_review": "장마감 리뷰",
            "premarket_review": "장전 검수",
        }.get(src, src)
        lines.append(f"    · {label}: ${d['cost_usd']:.4f} ({d['calls']}건)")
    lines.append(f"  이번 달: ${mo['total_usd']:.3f} ({mo['total_krw']:,}원)")
    if remaining is not None:
        pct = spent_cycle / budget * 100 if budget > 0 else 0
        lines.append(f"  충전 후 사용: ${spent_cycle:.3f}")
        lines.append(f"  잔여 크레딧: ${remaining:.2f} / ${budget:.2f} ({pct:.1f}% 소진)")
    else:
        lines.append(f"  누적 사용: ${total_spent():.3f}")
    return "\n".join(lines)
