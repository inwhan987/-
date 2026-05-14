"""종목코드 ↔ 회사명 조회.

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


# ── 이름 → 코드 역방향 조회 ───────────────────────────────────────────────────

_NAME_TO_CODE: dict[str, str] = {
    # 코스피 대형주
    "삼성전자": "005930.KS",
    "sk하이닉스": "000660.KS",
    "sk하이닉": "000660.KS",
    "하이닉스": "000660.KS",
    "현대차": "005380.KS",
    "현대자동차": "005380.KS",
    "기아": "000270.KS",
    "기아차": "000270.KS",
    "삼성바이오로직스": "207940.KS",
    "삼성바이오": "207940.KS",
    "셀트리온": "068270.KS",
    "lg화학": "051910.KS",
    "lg에너지솔루션": "373220.KS",
    "lg에너지": "373220.KS",
    "포스코홀딩스": "005490.KS",
    "posco": "005490.KS",
    "포스코": "005490.KS",
    "카카오": "035720.KS",
    "네이버": "035420.KS",
    "naver": "035420.KS",
    "삼성물산": "028260.KS",
    "현대모비스": "012330.KS",
    "kb금융": "105560.KS",
    "신한지주": "055550.KS",
    "하나금융지주": "086790.KS",
    "우리금융지주": "316140.KS",
    "삼성생명": "032830.KS",
    "삼성화재": "000810.KS",
    "sk텔레콤": "017670.KS",
    "kt": "030200.KS",
    "lg유플러스": "032640.KS",
    "롯데케미칼": "011170.KS",
    "한화에어로스페이스": "012450.KS",
    "한화에어로": "012450.KS",
    "두산에너빌리티": "034020.KS",
    "한국전력": "015760.KS",
    "한전": "015760.KS",
    "고려아연": "010130.KS",
    "현대건설": "000720.KS",
    "삼성엔지니어링": "028050.KS",
    "sk이노베이션": "096770.KS",
    "에코프로비엠": "247540.KS",
    "에코프로": "086520.KS",
    "엘앤에프": "066970.KS",
    # 코스닥
    "카카오뱅크": "323410.KS",
    "크래프톤": "259960.KS",
    "하이브": "352820.KS",
    "카카오게임즈": "293490.KQ",
    "펄어비스": "263750.KQ",
}


def find_code(name: str) -> str | None:
    """회사명(한글/영문) → 종목코드. 없으면 None.

    대소문자·공백 무시, 부분 일치도 지원.
    예) "삼성전자" → "005930.KS"
        "하이닉스" → "000660.KS"
    """
    key = name.strip().lower().replace(" ", "")
    # 1) 정확히 일치
    if key in _NAME_TO_CODE:
        return _NAME_TO_CODE[key]
    # 2) 부분 일치 (키가 입력에 포함되거나, 입력이 키에 포함)
    for k, code in _NAME_TO_CODE.items():
        if key in k or k in key:
            return code
    # 3) 캐시에서 역방향 탐색 (get_name()으로 이미 조회된 코드)
    with _lock:
        for code, cname in _cache.items():
            if key in cname.lower().replace(" ", ""):
                suffix = ".KS" if len(code) == 6 else ""
                return code + suffix
    return None


def resolve_symbol(token: str) -> str:
    """종목코드 또는 종목명을 종목코드(xxx.KS)로 변환.

    이미 코드 형태면 그대로 반환.
    """
    token = token.strip()
    # 숫자 6자리 또는 xxx.KS 형태면 코드로 간주
    if re.fullmatch(r"\d{6}(\.KS|\.KQ)?", token, re.IGNORECASE):
        return token if "." in token else token + ".KS"
    code = find_code(token)
    return code if code else token
