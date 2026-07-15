"""네이버 fchart 분봉(종가) 조회 — 라이브 러너 '어제 봉' 워밍업 전용.

KIS 당일분봉 TR(FHKST03010200)은 오늘 것만 준다. 장초반(예: 9:40)에
RSI/볼린저/MACD/EMA120 같은 '종가 기반' 지표를 데우려면 전일(+그 이전) 봉이
필요한데, 모의(paper) 서버는 과거 분봉 TR(FHKST03010230)을 막아둔다.

대안으로 네이버 fchart(`timeframe=minute`)를 쓴다. 이 엔드포인트는
약 6거래일치 1분봉을 주지만 **종가만** 유효하다(O/H/L 은 null, 거래량은 누적).

용도 2가지:
1. `fetch_prev_closes` — 종가 시리즈 워밍업 (RSI/볼린저/MACD/EMA120).
2. `fetch_prev_ohlcv` — 1분 종가 5개를 묶어 N분봉 '유사 OHLC'를 합성
   (o=빈 첫 종가, h=max, l=min, c=막 종가) → ST/PSAR/HTF-ADX 히스토리 워밍업.
   실제 고저보다 폭이 약간 좁다(1분 내 극값 누락). 2026-07-15 검증:
   실 5분봉 대비 ST(7,3) 방향 일치율 95.6~100% (당일봉만 쓰던 기존 67~88%).
   VWAP/ATR(손절)는 여전히 '당일 실봉'만 사용 — 여기 데이터 안 씀.

설계 원칙
---------
- 어제 종가는 "부족분(deficit)"만 앞에 붙인다. 9:40 5분봉 기준 오늘 봉이 8개면,
  20봉 필요한 지표는 어제서 12개만 빌린다.
- 실패(네트워크/파싱)해도 절대 예외를 올리지 않고 [] 를 돌려준다 → 라이브 무중단.
- 오늘 데이터는 KIS 가 책임지므로, 여기서는 '오늘 이전' 봉만 반환한다.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from loguru import logger

try:  # httpx 는 프로젝트 의존성이지만 방어적으로 import
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

_FCHART_URL = "https://fchart.stock.naver.com/sise.nhn"
_UA = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.naver.com/",
}
# fchart 1분봉 최대 ~6거래일. EMA120(=5분봉 120개=1분 600개≈1.5일)까지 여유.
_DEFAULT_COUNT = 3000
_VALID_INTERVALS = (1, 3, 5, 10, 15, 30, 60)


def _code6(symbol: str) -> str:
    """'005930.KS' → '005930'."""
    return symbol.split(".")[0]


def _parse_fchart(text: str) -> pd.DataFrame:
    """fchart XML → DataFrame(index=DatetimeIndex, col=close). 종가만 유효."""
    import re

    rows: list[tuple[datetime, float]] = []
    for raw in re.findall(r'data="([^"]+)"', text):
        parts = raw.split("|")
        if len(parts) < 5:
            continue
        ts_str, close_str = parts[0], parts[4]
        if not ts_str or not close_str or close_str in ("null", "None"):
            continue
        try:
            ts = datetime.strptime(ts_str[:12], "%Y%m%d%H%M")
            close = float(close_str)
        except (ValueError, TypeError):
            continue
        if close <= 0:
            continue
        rows.append((ts, close))
    if not rows:
        return pd.DataFrame(columns=["close"])
    df = pd.DataFrame(rows, columns=["ts", "close"]).set_index("ts").sort_index()
    return df[~df.index.duplicated(keep="last")]


def _resample_close(df1: pd.DataFrame, interval: int) -> pd.DataFrame:
    """1분 종가 → N분 종가(bin 마지막 종가). origin=start_day 로 09:00 정렬."""
    if interval <= 1:
        return df1
    return (
        df1.resample(f"{interval}min", label="left", closed="left", origin="start_day")
        .agg({"close": "last"})
        .dropna(subset=["close"])
    )


def _resample_pseudo_ohlc(df1: pd.DataFrame, interval: int) -> pd.DataFrame:
    """1분 종가 → N분 '유사 OHLC' (o=첫 종가, h=max, l=min, c=막 종가, vol=0)."""
    g = df1["close"].resample(
        f"{interval}min", label="left", closed="left", origin="start_day"
    )
    out = pd.DataFrame(
        {"open": g.first(), "high": g.max(), "low": g.min(), "close": g.last()}
    )
    out["volume"] = 0.0  # fchart 거래량은 누적치라 봉별 거래량으로 못 씀 → 0 표기
    return out.dropna(subset=["close"])


def _fetch_prev_1min(
    symbol: str, today: str | None, count: int, timeout: float
) -> pd.DataFrame:
    """오늘 이전 1분 종가 DataFrame. 실패/데이터없음 → 빈 DataFrame (무중단)."""
    code = _code6(symbol)
    today = today or datetime.now().strftime("%Y%m%d")
    url = f"{_FCHART_URL}?symbol={code}&timeframe=minute&count={count}&requestType=0"
    try:
        resp = httpx.get(url, headers=_UA, timeout=timeout)
        resp.raise_for_status()
        df1 = _parse_fchart(resp.text)
    except Exception as exc:  # noqa: BLE001 — 라이브 무중단
        logger.warning("naver_minute: {} fchart 실패: {}", code, exc)
        return pd.DataFrame(columns=["close"])
    if df1.empty:
        return df1
    # 오늘 봉 제외 (오늘은 KIS 실 OHLC 가 담당)
    try:
        today_dt = datetime.strptime(today, "%Y%m%d").date()
        df1 = df1[df1.index.date < today_dt]
    except Exception:  # noqa: BLE001
        pass
    return df1


def fetch_prev_closes(
    symbol: str,
    interval_min: int = 5,
    need: int = 0,
    *,
    today: str | None = None,
    count: int = _DEFAULT_COUNT,
    timeout: float = 8.0,
) -> list[float]:
    """오늘 이전(어제+그 이전) N분봉 종가를 부족분(need)만 오름차순으로 반환.

    Parameters
    ----------
    symbol : 종목코드 ('005930' 또는 '005930.KS').
    interval_min : 목표 분봉 간격 (1/3/5/10/15/30/60).
    need : 필요한 봉 수(부족분). 0 이하이면 빈 리스트.
    today : 'YYYYMMDD'. 이 날짜(오늘) 봉은 제외. 기본=오늘.

    Returns
    -------
    list[float] : 가장 최근(어제 마지막)으로 끝나는 오름차순 종가 리스트.
                  실패 시 [].
    """
    if need <= 0 or httpx is None:
        return []
    if interval_min not in _VALID_INTERVALS:
        # 540/interval 이 정수가 아니면 09:00 정렬이 깨질 수 있어 차단
        logger.debug("naver_minute: 미지원 간격 {} → skip", interval_min)
        return []

    df1 = _fetch_prev_1min(symbol, today, count, timeout)
    if df1.empty:
        return []

    dfn = _resample_close(df1, interval_min)
    if dfn.empty:
        return []

    closes = dfn["close"].astype(float).tolist()
    return closes[-need:] if need < len(closes) else closes


def fetch_prev_ohlcv(
    symbol: str,
    interval_min: int = 5,
    need: int = 0,
    *,
    today: str | None = None,
    count: int = _DEFAULT_COUNT,
    timeout: float = 8.0,
) -> list[dict]:
    """오늘 이전 N분봉 '유사 OHLC'를 부족분(need)만 오름차순 dict 리스트로 반환.

    1분 종가를 빈(bin) 단위로 합성한 근사봉 — ST/PSAR/HTF-ADX 히스토리 워밍업 전용.
    스크리너가 매일 종목을 바꿔도 어떤 종목이든 즉시 어제봉을 확보할 수 있고,
    상태 파일이 필요 없다. volume=0 이므로 거래량 지표에는 걸리지 않게 할 것.

    Returns
    -------
    list[dict] : [{"open","high","low","close","volume"}] 오름차순. 실패 시 [].
    """
    if need <= 0 or httpx is None:
        return []
    if interval_min not in _VALID_INTERVALS:
        logger.debug("naver_minute: 미지원 간격 {} → skip", interval_min)
        return []

    df1 = _fetch_prev_1min(symbol, today, count, timeout)
    if df1.empty:
        return []

    dfn = _resample_pseudo_ohlc(df1, interval_min)
    if dfn.empty:
        return []
    if need < len(dfn):
        dfn = dfn.iloc[-need:]
    return dfn.astype(float).to_dict("records")
