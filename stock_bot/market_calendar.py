"""KRX 거래일/휴장일 판정 (KIS 미사용 공용 헬퍼).

runner(KIS 휴장일 API 우선) 와 web(KIS 인증 없이) 가 공유.
exchange_calendars 가 모르는 임시공휴일(선거일 등)은 추가 휴장일로 보강한다.

추가 휴장일은 두 출처를 합친다:
  1) BASE_HOLIDAYS — 코드 기본값(배포 시 고정)
  2) data/extra_holidays.json — 웹 파라미터 탭에서 수동 입력(재시작 없이 반영)

두 컨테이너(stock-bot/stock-web)가 ./data 를 공유 마운트하므로 파일 변경이
양쪽에 즉시 반영된다. mtime 캐시로 파일 변경 시에만 다시 읽는다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# 코드 기본값(배포 고정). 웹에서 지운 뒤에도 남아있는 안전망.
BASE_HOLIDAYS: set[str] = {
    "2026-06-03",  # 제9회 전국동시지방선거 (임시공휴일)
}

# 컨테이너에서는 /app/data, 로컬에서는 repo/data
_DATA_DIR = Path("/app/data") if Path("/app/data").exists() else (Path(__file__).resolve().parents[1] / "data")
HOLIDAYS_FILE = _DATA_DIR / "extra_holidays.json"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_cache: dict = {"mtime": None, "set": set()}


def _normalize(d: str) -> str | None:
    """'YYYY-MM-DD' 형식만 통과. 유효 날짜인지까지 검증."""
    d = str(d).strip()
    if not _DATE_RE.match(d):
        return None
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return None
    return d


def load_user_holidays() -> set[str]:
    """data/extra_holidays.json 의 수동 휴장일. mtime 변경 시에만 재읽기."""
    try:
        mtime = HOLIDAYS_FILE.stat().st_mtime
    except OSError:
        _cache["mtime"], _cache["set"] = None, set()
        return set()
    if _cache["mtime"] == mtime:
        return _cache["set"]
    try:
        data = json.loads(HOLIDAYS_FILE.read_text(encoding="utf-8"))
        s = {n for d in data if (n := _normalize(d))}
    except Exception:
        s = set()
    _cache["mtime"], _cache["set"] = mtime, s
    return s


def get_extra_holidays() -> set[str]:
    """코드 기본값 + 수동 입력 합집합."""
    return BASE_HOLIDAYS | load_user_holidays()


def save_user_holidays(dates) -> list[str]:
    """수동 휴장일 목록을 파일에 저장(정렬·중복제거·검증). 저장된 리스트 반환."""
    cleaned = sorted({n for d in dates if (n := _normalize(d))})
    HOLIDAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOLIDAYS_FILE.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache["mtime"] = None  # 캐시 무효화 → 다음 호출 시 재읽기
    return cleaned


def is_trading_day(date: datetime) -> bool:
    """KRX 거래일 여부 (주말 + 임시공휴일 + 정규공휴일). KIS 미사용.

    1) 주말 → 휴장
    2) 추가 휴장일(기본값+수동입력) 등록일 → 휴장
    3) exchange_calendars 판정 (라이브러리 실패 시 주말 여부로 폴백)
    """
    if date.weekday() >= 5:
        return False
    ds = date.strftime("%Y-%m-%d")
    if ds in get_extra_holidays():
        return False
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XKRX")
        return bool(cal.is_session(ds))
    except Exception:
        return date.weekday() < 5
