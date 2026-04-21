"""종목코드 → 회사명 조회.

Naver 금융 메인 페이지에서 회사명을 파싱해 프로세스 캐시에 저장한다.
실패하거나 오프라인이면 빈 문자열을 반환해 UI가 깨지지 않도록 한다.
"""
from __future__ import annotations

import re
from threading import Lock

import httpx
from bs4 import BeautifulSoup
from loguru import logger

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_NAVER_MAIN = "https://finance.naver.com/item/main.naver"

_cache: dict[str, str] = {}
_lock = Lock()


def _strip_suffix(symbol: str) -> str:
    """'005930.KS' → '005930'. yfinance 포맷도 지원."""
    return symbol.split(".")[0]


def _fetch_from_naver(symbol: str) -> str:
    try:
        r = httpx.get(
            _NAVER_MAIN,
            params={"code": symbol},
            headers={"User-Agent": _USER_AGENT},
            timeout=5.0,
        )
        r.raise_for_status()
        # 메인 페이지는 UTF-8 (뉴스 목록 페이지와 다름). httpx 가 헤더의 charset 을 보고 고르도록 r.text 사용.
        soup = BeautifulSoup(r.text, "lxml")
        tag = soup.select_one("div.wrap_company h2 a")
        if tag:
            return tag.get_text(strip=True)
    except Exception as exc:
        logger.debug("name lookup failed for {}: {}", symbol, exc)
    return ""


def get_name(symbol: str) -> str:
    """종목코드의 회사명. 실패 시 빈 문자열."""
    if not symbol:
        return ""
    key = _strip_suffix(symbol)
    # 한국 종목코드만(6자리 숫자) 처리. 해외티커는 그대로 리턴.
    if not re.fullmatch(r"\d{6}", key):
        return ""
    with _lock:
        if key in _cache:
            return _cache[key]
    name = _fetch_from_naver(key)
    with _lock:
        _cache[key] = name
    return name


def prime(symbols: list[str]) -> None:
    """여러 종목명을 미리 조회해 캐시에 채운다."""
    for s in symbols:
        get_name(s)
