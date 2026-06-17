"""네이버 실시간 시세 폴링 — 대시보드 현재가 '표시' 전용.

KIS inquire-price 는 모의(paper) 1건/초 한도를 웹·스톡봇·대장주봇 3프로세스가
공유(파일락)해, 종목 직렬 조회 + 간헐 지연/재시도가 프론트 8초 타임아웃을 넘겨
대시보드에 '갱신 지연' 배지가 뜨곤 했다.

네이버 polling.finance.naver.com 실시간 시세는:
  · 인증 불필요·KIS 유량 한도와 완전 분리
  · delayTime=0 (실시간, 지연 아님)
  · query=SERVICE_ITEM:코드1,코드2,... 로 **한 번에 여러 종목** (응답 수십 ms)
이라 현재가 표시에 적합하다. 매매 판단엔 절대 쓰지 않고 화면 표시 전용.
실패해도 예외를 올리지 않고 빈 dict 를 돌려준다 → 호출측이 직전값을 유지.
"""
from __future__ import annotations

import json

from loguru import logger

try:  # httpx 는 프로젝트 의존성이지만 방어적으로 import
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

_URL = "https://polling.finance.naver.com/api/realtime"
_UA = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.naver.com/",
}
# rf(등락 구분): 1 상한·2 상승 → +, 4 하한·5 하락 → -, 3 보합 → 0
_DOWN = {"4", "5"}


def _code6(symbol: str) -> str:
    """'005930.KS' → '005930'."""
    return symbol.split(".")[0]


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_quotes(symbols, *, timeout: float = 4.0) -> dict[str, dict]:
    """여러 종목 현재가를 한 번의 호출로 조회.

    반환: {code6: {price, change, change_pct, status, open, high, low, volume}}.
    실패(네트워크/파싱) 시 빈 dict — 호출측에서 직전값 유지(무중단).
    """
    if httpx is None or not symbols:
        return {}
    codes = [_code6(s) for s in symbols]
    query = "SERVICE_ITEM:" + ",".join(codes)
    try:
        r = httpx.get(_URL, params={"query": query}, headers=_UA, timeout=timeout)
        r.raise_for_status()
        # 응답 본문(종목명 nm)이 EUC-KR 이라 httpx 의 utf-8 .json() 이 깨진다.
        # nm 은 쓰지 않지만 파싱 자체가 실패하므로 명시 디코딩 후 json.loads.
        try:
            text = r.content.decode("euc-kr")
        except UnicodeDecodeError:
            text = r.content.decode("utf-8", errors="replace")
        body = json.loads(text)
    except Exception as exc:  # noqa: BLE001 — 표시 전용, 무중단
        logger.warning("naver_quote 실패: {}", exc)
        return {}

    out: dict[str, dict] = {}
    try:
        for area in body.get("result", {}).get("areas", []):
            for d in area.get("datas", []):
                code = d.get("cd")
                price = _f(d.get("nv"))           # now value = 현재가
                if not code or price is None:
                    continue
                sign = -1 if str(d.get("rf") or "3") in _DOWN else 1
                out[code] = {
                    "price": price,
                    "change": abs(_f(d.get("cv")) or 0.0) * sign,    # 전일대비
                    "change_pct": abs(_f(d.get("cr")) or 0.0) * sign,  # 등락률
                    "status": d.get("ms") or "",                     # OPEN/CLOSE
                    "open": _f(d.get("ov")),
                    "high": _f(d.get("hv")),
                    "low": _f(d.get("lv")),
                    "volume": int(d.get("aq") or 0),
                }
    except Exception as exc:  # noqa: BLE001
        logger.warning("naver_quote 파싱 실패: {}", exc)
    return out
