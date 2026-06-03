"""KRX 거래일/휴장일 판정 (KIS 미사용 공용 헬퍼).

runner(KIS 휴장일 API 우선) 와 web(KIS 인증 없이) 가 공유.
exchange_calendars 가 모르는 임시공휴일(선거일 등)은 EXTRA_HOLIDAYS 로 보강한다.
"""
from __future__ import annotations

from datetime import datetime

# exchange_calendars 가 누락하는 임시공휴일(선거일 등) 수동 보강. YYYY-MM-DD.
EXTRA_HOLIDAYS: set[str] = {
    "2026-06-03",  # 제9회 전국동시지방선거 (임시공휴일)
}


def is_trading_day(date: datetime) -> bool:
    """KRX 거래일 여부 (주말 + 임시공휴일 + 정규공휴일). KIS 미사용.

    1) 주말 → 휴장
    2) EXTRA_HOLIDAYS 등록일 → 휴장
    3) exchange_calendars 판정 (라이브러리 실패 시 주말 여부로 폴백)
    """
    if date.weekday() >= 5:
        return False
    ds = date.strftime("%Y-%m-%d")
    if ds in EXTRA_HOLIDAYS:
        return False
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XKRX")
        return bool(cal.is_session(ds))
    except Exception:
        return date.weekday() < 5
