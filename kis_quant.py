"""KIS 거래대금 순위 모듈 — 대장주 선별 유니버스 소스(네이버 대체 후보).

네이버 sise_quant 은 거래대금이 KRX/NXT 로 분리돼 고가·고거래대금 종목
(레인보우로보틱스 등)이 순위에서 누락된다. KIS 로 '통합(UN=KRX+NXT) 거래대금'
기준 500억+ 종목을 코스피·코스닥 빠짐없이 회수한다.

문제: KIS volume-rank(FHPST01710000, 거래금액순)는 거래소별(J/NX)로만 조회되고
통합(UN)은 미지원 + 거래소당 최대 30행. 한쪽 리스트에만 든 종목은 다른 거래소분을
못 더해 과소계상되고, 양쪽 30위 밖이면 통째로 누락된다.

해법(2단계):
  ① volume-rank J·NX × 코스피·코스닥 → 후보 코드 union 수집(거래소당 30 → 시장별 ≤60)
  ② 각 후보의 '진짜 통합 거래대금'을 inquire-price(FID_COND_MRKT_DIV_CODE=UN)로 재조회.
     검증: 레인보우 J878+NX1,192=UN2,070 정확 일치.
  · 유량 절감: 양쪽 거래소에 다 든 종목은 J+NX 합=통합이라 재조회 불요.
    한쪽에만 든 종목도 (관측합 + 반대거래소 30위컷) < 500억이면 절대 미달 → 스킵.
    경계 종목만 UN 재조회 → 보통 20~30콜(1회성 오전 선별, 모의 1/초 게이트 준수).

반환 스키마는 naver_quant.fetch_ranking 과 동일:
  code,name,price,change_pct,volume,value_won,market_cap,market.
fetch_ranking 의 드롭인 대체 — 소스 전환은 leader_finder 에서 토글.
"""
from __future__ import annotations

import pandas as pd

from naver_quant import _is_common_stock  # 보통주 필터 재사용

_RANK_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
_RANK_TR = "FHPST01710000"
_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
_PRICE_TR = "FHKST01010100"
_INVESTOR_HIST_PATH = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
_INVESTOR_HIST_TR = "FHPTJ04160001"
_MARKETS = (("0001", "KOSPI"), ("1001", "KOSDAQ"))
_VENUES = ("J", "NX")  # KRX, NXT


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


def fetch_investor_netbuy(broker, codes: list[str]) -> dict:
    """종목 리스트의 당일 기관+외국인 순매수수량 합산(KIS FHKST01010100 UN 조회).

    반환: {code: float(성공) | None(실패)}. None 은 조회 실패 sentinel —
    실제 순매수 0주(float 0.0)와 구분해 호출부에서 수급 가중치 제거 여부를 판단.
    KIS 레이트 리밋: 선별 1회성 호출이므로 모의 1/초 규정 내 직렬 처리.
    """
    result: dict = {}
    for code in codes:
        try:
            params = {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code}
            resp = broker._get_with_retry(
                _PRICE_PATH, _PRICE_TR, params, label=f"flow {code}", attempts=2)
            o = resp.json().get("output", {}) or {}
            frgn = int(o.get("frgn_ntby_qty") or 0)
            orgn = int(o.get("orgn_ntby_qty") or 0)
            result[code] = float(frgn + orgn)
        except Exception:
            result[code] = None  # 실패 sentinel — 0.0(진짜 순매수 0)과 구분
    return result


def fetch_investor_history_5d(broker, codes: list[str], today_yyyymmdd: str) -> dict:
    """종목별 최근 5거래일 중 연속 순매수일수(기관+외국인).

    KIS FHPTJ04160001(investor-trade-by-stock-daily) UN 모드 사용.
    반환: {code: int(0~5 연속순매수일수) | None(조회 실패)}.
    None = 조회 실패 sentinel.  0 = 당일 포함 연속 순매수 없음.
    """
    result: dict = {}
    for code in codes:
        try:
            params = {
                "FID_COND_MRKT_DIV_CODE": "UN",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": today_yyyymmdd,
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "",
            }
            resp = broker._get_with_retry(
                _INVESTOR_HIST_PATH, _INVESTOR_HIST_TR, params,
                label=f"hist5d {code}", attempts=2)
            rows = resp.json().get("output2") or []
            # 날짜 내림차순 최대 5일
            days = sorted(rows, key=lambda r: r.get("stck_bsop_date", ""), reverse=True)[:5]
            consecutive = 0
            for row in days:
                net = int(row.get("frgn_ntby_qty") or 0) + int(row.get("orgn_ntby_qty") or 0)
                if net > 0:
                    consecutive += 1
                else:
                    break
            result[code] = consecutive
        except Exception:
            result[code] = None
    return result


def fetch_ranking(top_n: int = 100, stock_only: bool = True,
                  broker=None, min_value: float = 500e8) -> pd.DataFrame:
    """KIS 통합(KRX+NXT) 거래대금 상위 종목. naver_quant.fetch_ranking 드롭인.

    min_value: 통합 거래대금 하한(원, 기본 500억). 이 값을 넘는 코스피·코스닥
      종목을 빠짐없이 회수하도록 UN 재조회 프루닝 기준으로 쓴다(값 자체는 필터하지
      않음 — 게이트는 leader_finder 가 담당).
    top_n: 시그니처 호환용(절단 없음 — KIS 랭킹이 거래소당 30행으로 이미 한정).
    broker: 재사용할 KISBroker(없으면 내부 생성; 토큰 디스크 캐시 공유).
    """
    own = False
    if broker is None:
        from stock_bot.broker import KISBroker
        broker = KISBroker()
        own = True
    try:
        # ① 후보 union 수집 + 거래소별 30위컷
        cand: dict[str, dict] = {}
        cut: dict[tuple[str, str], float] = {}
        for iscd, mlabel in _MARKETS:
            for venue in _VENUES:
                try:
                    rows = _fetch_venue(broker, venue, iscd)
                except Exception as e:
                    print(f"  [KIS {mlabel}/{venue} 거래대금 실패] {e}")
                    continue
                vmin = None
                for r in rows:
                    code = str(r.get("mksc_shrn_iscd") or "").strip()
                    if len(code) != 6:
                        continue
                    try:
                        val = float(r.get("acml_tr_pbmn") or 0)   # 원
                        price = float(r.get("stck_prpr") or 0)
                        chg = float(r.get("prdy_ctrt") or 0)
                        vol = float(r.get("acml_vol") or 0)
                        shares = float(r.get("lstn_stcn") or 0)
                    except (TypeError, ValueError):
                        continue
                    vmin = val if vmin is None else min(vmin, val)
                    e = cand.get(code)
                    if e is None:
                        cand[code] = {
                            "code": code,
                            "name": str(r.get("hts_kor_isnm") or "").strip(),
                            "market": mlabel, "price": price, "change_pct": chg,
                            "volume": vol, "market_cap": price * shares,
                            "venues": {venue: val},
                        }
                    else:
                        e["venues"][venue] = val
                        e["volume"] = max(e["volume"], vol)
                if vmin is not None:
                    cut[(mlabel, venue)] = vmin

        if not cand:
            return pd.DataFrame()

        # ② 통합 거래대금 확정(프루닝된 UN 재조회)
        un_calls = 0
        for code, e in cand.items():
            partial = sum(e["venues"].values())
            seen = set(e["venues"])
            missing = [v for v in _VENUES if v not in seen]
            if not missing:
                e["value_won"] = partial            # 양쪽 다 관측 → 합=통합(정확)
                continue
            if partial >= min_value:
                e["value_won"] = partial            # 이미 통과(통합은 더 큼) → 재조회 불요
                continue
            bound = sum(cut.get((e["market"], v), 0.0) for v in missing)
            if partial + bound < min_value:
                e["value_won"] = partial            # 최대치로도 미달 → 재조회 불요
                continue
            # 경계 종목만 정확한 통합값 조회
            try:
                uv, uchg, upx = _un_quote(broker, code)
                un_calls += 1
                e["value_won"] = uv if uv > 0 else partial
                if uchg:
                    e["change_pct"] = uchg
                if upx:
                    e["price"] = upx
            except Exception:
                e["value_won"] = partial

        df = pd.DataFrame([
            {k: e[k] for k in ("code", "name", "price", "change_pct",
                               "volume", "value_won", "market_cap", "market")}
            for e in cand.values()
        ])
        if stock_only:
            mask = df.apply(lambda r: _is_common_stock(r["code"], r["name"]), axis=1)
            df = df[mask].copy()
        if un_calls:
            print(f"  [KIS 거래대금] UN 재조회 {un_calls}콜")
        return df.sort_values("value_won", ascending=False).reset_index(drop=True)
    finally:
        if own:
            broker.close()
