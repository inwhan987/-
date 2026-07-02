"""네이버 fchart 일봉 지수 레짐 판정 — 라이브 '베어장 신규 미진입' 게이트 전용.

배경
----
스톡봇 앙상블은 평균회귀(눌림목 매수)라, 지속 하락장(예: 코스닥 −20%)에서
계속 진입했다가 손절당해 출혈한다. 이를 막기 위해 **종목이 속한 시장지수**의
일봉 레짐이 '베어'면 그 종목의 신규 매수를 차단한다.

베어 판정 (AND)
--------------
- 종가 < MA_PERIOD일 이동평균  (추세선 아래)
- 10일 모멘텀 < 0             (하락 지속)
둘 다 충족해야 베어. AND 이므로 단발 폭락(MA 위)은 통과 → 눌림목 매수 유지.

데이터
------
네이버 fchart(`timeframe=day`, 심볼 KOSPI/KOSDAQ). KIS는 일봉 지수 히스토리를
주지 않아 fchart 를 쓴다. 종가만 사용(이동평균·모멘텀 모두 종가 기반).
- 하루 1회/시장 호출 후 KST 날짜로 캐싱 → KIS 유량 무관, 외부호출 최소.
- 실패/데이터부족 시 절대 예외를 올리지 않고 False(=통과) 반환 → 라이브 무중단.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

try:  # httpx 는 프로젝트 의존성이지만 방어적으로 import
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

_KST = ZoneInfo("Asia/Seoul")
_FCHART_URL = "https://fchart.stock.naver.com/sise.nhn"
_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}

# 판정 상수 (표본 부족으로 튜닝 근거 없음 → 코드 고정)
# 2026-07-02: 50→20 하향 — 50MA 는 반응이 느려 급락장 진입 차단이 늦음(사용자 지시)
MA_PERIOD = 20      # 이동평균 기간(일)
MOM_DAYS = 10       # 모멘텀 룩백(일)
_COUNT = 80         # fchart 일봉 요청 개수 (50MA + 여유)

# 시장코드 → 네이버 fchart 심볼
_NAVER_SYM = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ"}

# 일별 캐시: {(market, "YYYYMMDD"): bool}
_cache: dict[tuple[str, str], bool] = {}

# 종목코드 → 시장("KOSPI"|"KOSDAQ") 캐시 — 상장시장은 사실상 불변이라 프로세스 수명 캐시
_mkt_cache: dict[str, str] = {}
_NAVER_BASIC = "https://m.stock.naver.com/api/stock/{code}/basic"


def stock_market(code: str) -> str | None:
    """6자리 종목코드 → "KOSPI"|"KOSDAQ" (실패 시 None).

    settings.symbols 가 suffix 없는 6자리 코드라 .KQ 판별이 불가능해
    레짐 게이트가 전 종목을 코스피로 오판하던 버그의 보완용.
    네이버 종목 basic API 를 종목당 1회만 호출하고 캐싱.
    """
    code = str(code or "").split(".")[0].strip()
    if not re.fullmatch(r"\d{6}", code) or httpx is None:
        return None
    if code in _mkt_cache:
        return _mkt_cache[code]
    try:
        r = httpx.get(_NAVER_BASIC.format(code=code), headers=_UA, timeout=8.0)
        r.raise_for_status()
        name = ((r.json().get("stockExchangeType") or {}).get("name") or "").upper()
    except Exception as exc:  # noqa: BLE001 — 라이브 무중단
        logger.warning("naver_index: {} 시장구분 조회 실패: {}", code, exc)
        return None
    if name in ("KOSPI", "KOSDAQ"):
        _mkt_cache[code] = name
        return name
    return None


def _fetch_closes(naver_sym: str, *, timeout: float = 8.0) -> list[float]:
    """fchart 일봉 종가 오름차순 리스트. 실패 시 []."""
    if httpx is None:
        return []
    url = f"{_FCHART_URL}?symbol={naver_sym}&timeframe=day&count={_COUNT}&requestType=0"
    try:
        resp = httpx.get(url, headers=_UA, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — 라이브 무중단
        logger.warning("naver_index: {} fchart 실패: {}", naver_sym, exc)
        return []
    closes: list[float] = []
    for raw in re.findall(r'data="([^"]+)"', resp.text):
        parts = raw.split("|")
        if len(parts) < 5:
            continue
        cs = parts[4]
        if not cs or cs in ("null", "None"):
            continue
        try:
            c = float(cs)
        except (ValueError, TypeError):
            continue
        if c > 0:
            closes.append(c)
    return closes


def _is_bear(closes: list[float]) -> bool:
    """종가 시리즈 → 베어(MA_PERIOD일선 아래 AND 10일모멘텀−) 여부."""
    if len(closes) < MA_PERIOD + 1 or len(closes) < MOM_DAYS + 1:
        return False  # 데이터부족 → 통과
    cur = closes[-1]
    ma = sum(closes[-MA_PERIOD:]) / MA_PERIOD
    mom = cur / closes[-(MOM_DAYS + 1)] - 1.0
    return cur < ma and mom < 0.0


def regime_blocks(market: str, *, today: str | None = None) -> bool:
    """`market`("KOSPI"|"KOSDAQ") 지수가 베어면 True(=신규매수 차단).

    하루 1회/시장만 네이버를 호출하고 KST 날짜로 캐싱. 실패 시 False(통과).
    """
    naver_sym = _NAVER_SYM.get((market or "").upper())
    if not naver_sym:
        return False  # 미지원 시장 → 통과
    day = today or datetime.now(tz=_KST).strftime("%Y%m%d")
    key = (naver_sym, day)
    if key in _cache:
        return _cache[key]
    bear = _is_bear(_fetch_closes(naver_sym))
    _cache[key] = bear
    if bear:
        logger.info("naver_index: {} 일봉 베어({}MA아래 & {}일모멘텀−) → 신규매수 차단",
                    naver_sym, MA_PERIOD, MOM_DAYS)
    return bear


# 대시보드 스냅샷 캐시: {naver_sym: (monotonic_ts, dict)} — 짧은 TTL로 장중 현재값 반영
_snap_cache: dict[str, tuple[float, dict]] = {}
_SNAP_TTL = 60.0  # 초. 네이버 호출 최소화 + 장중 현재값 갱신 균형


def market_snapshot(market: str, *, spark_n: int = 30) -> dict:
    """대시보드용 지수 현황 스냅샷.

    반환: {ok, market, value, prev, change, change_pct, closes(스파크라인용),
           ma50, mom10_pct, is_bear}. 60초 TTL 캐시(장중 현재값 반영 + 호출 최소).
    실패 시 {ok: False, market}. 라이브 게이트(`regime_blocks`)와 독립.
    """
    naver_sym = _NAVER_SYM.get((market or "").upper())
    if not naver_sym:
        return {"ok": False, "market": market}
    now = time.monotonic()
    hit = _snap_cache.get(naver_sym)
    if hit and now - hit[0] < _SNAP_TTL:
        return hit[1]
    closes = _fetch_closes(naver_sym)
    if len(closes) < 2:
        return {"ok": False, "market": naver_sym}  # 실패는 캐싱 안 함(다음 호출 재시도)
    cur = closes[-1]
    prev = closes[-2]
    change = cur - prev
    change_pct = (cur / prev - 1.0) * 100.0 if prev else 0.0
    ma50 = sum(closes[-MA_PERIOD:]) / MA_PERIOD if len(closes) >= MA_PERIOD else None
    mom10 = (cur / closes[-(MOM_DAYS + 1)] - 1.0) * 100.0 if len(closes) >= MOM_DAYS + 1 else None
    snap = {
        "ok": True,
        "market": naver_sym,
        "value": round(cur, 2),
        "prev": round(prev, 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "closes": [round(c, 2) for c in closes[-spark_n:]],
        "ma50": round(ma50, 2) if ma50 is not None else None,
        "mom10_pct": round(mom10, 2) if mom10 is not None else None,
        "is_bear": _is_bear(closes),
    }
    _snap_cache[naver_sym] = (now, snap)
    return snap
