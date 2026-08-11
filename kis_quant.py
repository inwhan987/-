"""KIS 거래대금 순위 모듈 — 매경 폴백. KRX 단독 정책(2026-08-10).

매경(mk_quant) 이 primary. 매경 실패(사이트 다운/파싱 오류) 시 KIS 로 폴백한다.
정책: KRX(FID_COND_MRKT_DIV_CODE=J) 만 조회하고 NXT/UN 재조회는 사용하지 않는다
— 사용자 결정. 네이버 테마 거래대금과 매경 거래대금이 모두 KRX 기준이라 유니버스
스케일을 일관되게 KRX 로 통일한다.

동작:
  · volume-rank J × 코스피·코스닥 → 각 30행 회수 → 병합 → ETF/우선주 제외.
  · 반환 스키마는 naver_quant.fetch_ranking 과 동일:
    code,name,price,change_pct,volume,value_won,market_cap,market.
"""
from __future__ import annotations

import pandas as pd

from naver_quant import _is_common_stock  # 보통주 필터 재사용

_RANK_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
_RANK_TR = "FHPST01710000"
_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
_PRICE_TR = "FHKST01010100"
_DAILY_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
_DAILY_CHART_TR = "FHKST03010100"
_MARKETS = (("0001", "KOSPI"), ("1001", "KOSDAQ"))
_VENUE = "J"  # KRX 단독 (NXT 미사용 — 2026-08-10 정책)


def _fetch_venue(broker, mrkt: str, iscd: str) -> list[dict]:
    """거래소(mrkt=J/NX) × 시장(iscd) 거래대금(거래금액순) 상위 30행."""
    params = {
        "FID_COND_MRKT_DIV_CODE": mrkt,
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": iscd,
        "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "3",         # 3=거래금액(거래대금)순
        "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "0000000000",
        "FID_INPUT_PRICE_1": "",
        "FID_INPUT_PRICE_2": "",
        "FID_VOL_CNT": "",
    }
    resp = broker._get_with_retry(
        _RANK_PATH, _RANK_TR, params, label=f"valrank {mrkt}/{iscd}", attempts=3)
    j = resp.json()
    if str(j.get("rt_cd")) != "0":
        return []
    return j.get("output") or []


def _un_quote(broker, code: str) -> tuple[float, float, float]:
    """통합(UN) 현재가 조회 → (통합거래대금원, 등락률, 현재가). 실패 시 (0,0,0)."""
    params = {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code}
    resp = broker._get_with_retry(
        _PRICE_PATH, _PRICE_TR, params, label=f"un {code}", attempts=3)
    o = resp.json().get("output", {}) or {}
    return (float(o.get("acml_tr_pbmn") or 0),
            float(o.get("prdy_ctrt") or 0),
            float(o.get("stck_prpr") or 0))


def avg_value_5d_un(broker, code: str, today_yyyymmdd: str) -> float:
    """KIS KRX 일봉 최근 5거래일 평균 거래대금(원) — KRX 단독(2026-08-10 정책).

    inquire-daily-itemchartprice(FHKST03010100) 를 J(KRX) 모드로 호출해 종목당
    1콜로 21일치 일봉을 받아 당일(마지막 행, 미완성) 제외 직전 5거래일의
    acml_tr_pbmn 평균을 낸다. 유니버스(매경·KIS) 가 KRX 기준이므로 평소대비 배수
    분모도 KRX 로 통일. (함수명은 호환용으로 유지 — 실제 동작은 KRX.)
    """
    try:
        from datetime import datetime, timedelta
        start = (datetime.strptime(today_yyyymmdd, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": today_yyyymmdd,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        resp = broker._get_with_retry(
            _DAILY_CHART_PATH, _DAILY_CHART_TR, params,
            label=f"un-daily {code}", attempts=2)
        rows = resp.json().get("output2") or []
        vals = []
        for r in rows:
            v = float(r.get("acml_tr_pbmn") or 0)
            if v > 0:
                vals.append((r.get("stck_bsop_date", ""), v))
        vals.sort(key=lambda x: x[0], reverse=True)  # 최신순
        # 오늘(미완성) 제외 직전 5거래일
        exclude_today = [v for d, v in vals if d and d < today_yyyymmdd][:5]
        if len(exclude_today) < 1:
            return 0.0
        return sum(exclude_today) / len(exclude_today)
    except Exception:
        return 0.0


def fetch_ranking(top_n: int = 100, stock_only: bool = True,
                  broker=None, min_value: float = 500e8) -> pd.DataFrame:
    """KIS KRX 거래대금 상위 종목(폴백). naver_quant.fetch_ranking 드롭인.

    KRX(J) 단독 조회 — NXT/UN 재조회 없음(2026-08-10 정책). 코스피·코스닥
    각 30행씩 회수해 병합. 매경 primary 실패 시 폴백으로만 호출된다.

    top_n / min_value 는 시그니처 호환용(KIS 랭킹이 시장당 30행 상한이라
    실질 절단·필터 없음 — 게이트는 leader_finder 가 담당).
    """
    own = False
    if broker is None:
        from stock_bot.broker import KISBroker
        broker = KISBroker()
        own = True
    try:
        rows_all: list[dict] = []
        for iscd, mlabel in _MARKETS:
            try:
                rows = _fetch_venue(broker, _VENUE, iscd)
            except Exception as e:
                print(f"  [KIS {mlabel}/KRX 거래대금 실패] {e}")
                continue
            for r in rows:
                code = str(r.get("mksc_shrn_iscd") or "").strip()
                if len(code) != 6:
                    continue
                try:
                    val = float(r.get("acml_tr_pbmn") or 0)
                    price = float(r.get("stck_prpr") or 0)
                    chg = float(r.get("prdy_ctrt") or 0)
                    vol = float(r.get("acml_vol") or 0)
                    shares = float(r.get("lstn_stcn") or 0)
                except (TypeError, ValueError):
                    continue
                rows_all.append({
                    "code": code,
                    "name": str(r.get("hts_kor_isnm") or "").strip(),
                    "market": mlabel, "price": price, "change_pct": chg,
                    "volume": vol, "value_won": val,
                    "market_cap": price * shares,
                    "listed_shares": shares,
                })
        if not rows_all:
            return pd.DataFrame()
        df = pd.DataFrame(rows_all).drop_duplicates("code")
        if stock_only:
            mask = df.apply(lambda r: _is_common_stock(r["code"], r["name"]), axis=1)
            df = df[mask].copy()
        return df.sort_values("value_won", ascending=False).reset_index(drop=True)
    finally:
        if own:
            broker.close()
