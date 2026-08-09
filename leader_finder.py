"""대장주(섹터 리더) 탐색기 — 장중 거래대금 상위에서 주도 섹터/종목 추출.

알고리즘 (사용자 설계):
  1) 장 시작 후, 거래대금 상위 100 (코스피+코스닥 통합)
  2) 그중 많이 상승하는 종목 추림 (등락률 >= RISE_MIN_PCT)
  3) 추려진 상승 종목들의 섹터(네이버 업종) 집계 → 주도(핫) 섹터 식별
  4) 각 핫섹터 안에서 상승률 1위 종목 선정,
     단 거래대금이 평소(최근 5거래일 평균) 대비 VOL_MULT 배 이상일 것
     (장중이므로 세션 경과 비율로 평균을 보정해 비교)

데이터 소스:
  - 거래대금 순위/등락률/현재가 : 네이버 KRX+NXT 통합(nxt_sise_quant 합산, 1순위)
      · KIS 통합(KRX+NXT) 거래대금 (kis_quant) 은 폴백 — 3분 주기 reval 시 유량 부담
  - 종목 유니버스 필터(ETF/ETN/우선주 제외) : 코드/이름 규칙 (pykrx 티커목록
    엔드포인트가 빈 값을 반환해 사용 불가 → 보통주=코드 끝자리 0,
    ETF/ETN=브랜드 접두 이름으로 제외)
  - 5일 평균 거래대금            : pykrx 일봉(거래량×종가 근사) — pykrx OHLCV에
    거래대금 컬럼이 없어 거래량×종가로 일중 거래대금을 근사 (일 1회, 디스크 캐시)
  - 업종(섹터)                   : 네이버 coinfo 업종 (캐시)

※ 기존 screener.py 와 완전 독립 (import 안 함).
※ KIS 미사용 — 선별된 소수 대장주의 분봉/호가 확인은 별도 전략에서 KIS 로.

사용:
  python leader_finder.py                 # 10:00 까지 대기 후 1회 선별(기본)
  python leader_finder.py --at 10:30      # 10:30 에 선별
  python leader_finder.py --once          # 지금 즉시 1회(테스트)
  python leader_finder.py --once --ignore-hours --rise-min 2.5 --vol-mult 3
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

HERE = Path(__file__).parent
_CACHE_DIR = HERE / "data"
_AVGVAL_CACHE_PATH = _CACHE_DIR / "leader_avgval_cache.json"
_TREND_CACHE_PATH = _CACHE_DIR / "leader_trend_cache.json"
_MARKET_FLOW_PATH = _CACHE_DIR / "leader_market_flow.json"

# 거래대금 순위 소스 (2026-08-09 재교체):
#   1순위 네이버 KRX+NXT 통합(fetch_ranking_unified) — 시장×거래소별 페이지 4건 크롤링
#   후 종목코드 기준으로 KRX·NXT 거래대금 합산. KIS 통합값에 근접하면서 유량 무제한.
#   3분 주기 reval 도입으로 KIS UN 재조회 α건 부담이 stock-bot 체결 지연을 유발할
#   위험이 있어 스왑. 실시간성은 네이버 스크래핑 시차(수초) 감수.
#   2순위 KIS 통합(kis_quant.fetch_ranking) — 네이버 실패 시 폴백. 시장×거래소 4건
#   + 프루닝된 UN 재조회. 브로커 API라 유량 소모 있음.
#   3순위 네이버 KRX 단독(fetch_ranking) — NXT 누락 위험. 최후 blackout 방지용.
import kis_quant
from naver_quant import (  # noqa: F401  (재export: verify_today_leaders 등 기존 import 호환)
    _HDR,
    _ETF_PREFIXES,
    _fetch_naver_quant,
    _is_etf_etn,
    _is_common_stock,
    fetch_ranking,
    fetch_ranking_unified,
)

# 세션: 09:00 ~ 15:30 (390분)
_SESSION_START = (9, 0)
_SESSION_END = (15, 30)
_SESSION_MIN = 390

# ── 캐시 ────────────────────────────────────────────────────────────
_SECTOR_CACHE: dict[str, str] = {}        # code -> 업종명
_GROUP_CACHE: dict[str, str] = {}         # upjong_no -> 업종명
_AVGVAL_CACHE: dict[str, dict] = {}       # code -> {"date": "YYYYMMDD", "avg": float}
_TREND_CACHE: dict[str, dict] = {}        # code -> {"date": "YYYYMMDD", "val": {...}|None}
_UNIVERSE_CACHE: dict[str, set] = {}      # "stocks" -> set(code)


def _load_avgval_cache() -> None:
    global _AVGVAL_CACHE
    try:
        if _AVGVAL_CACHE_PATH.exists():
            _AVGVAL_CACHE = json.loads(_AVGVAL_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        _AVGVAL_CACHE = {}


def _save_avgval_cache() -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _AVGVAL_CACHE_PATH.write_text(
            json.dumps(_AVGVAL_CACHE, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _load_trend_cache() -> None:
    global _TREND_CACHE
    try:
        if _TREND_CACHE_PATH.exists():
            _TREND_CACHE = json.loads(_TREND_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        _TREND_CACHE = {}


def _save_trend_cache() -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _TREND_CACHE_PATH.write_text(
            json.dumps(_TREND_CACHE, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


# ── market_flow: 시장유동성 배수 캐시 ─────────────────────────────
# 매 선별 시점에 상위N 거래대금 합(base_sum, 원단위)을 날짜별로 기록.
# 다음날부터 최근 short일 평균 / 최근 long일 평균 비율을 min_value 배수로 사용.
def _load_market_flow() -> dict[str, float]:
    try:
        if _MARKET_FLOW_PATH.exists():
            data = json.loads(_MARKET_FLOW_PATH.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in data.items() if v}
    except Exception:
        pass
    return {}


def _save_market_flow(cache: dict[str, float]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _MARKET_FLOW_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
    except Exception:
        pass


def _record_market_flow(today_key: str, base_sum: float) -> None:
    """오늘 base_sum 을 캐시에 기록(장중 여러번 갱신 시 최댓값 유지 = 마감 근사)."""
    if not base_sum or base_sum <= 0:
        return
    cache = _load_market_flow()
    prev = float(cache.get(today_key, 0.0))
    if base_sum > prev:
        cache[today_key] = float(base_sum)
    _save_market_flow(cache)


def _compute_market_flow_multiplier(today_key: str, short: int, long: int,
                                     low: float, high: float) -> tuple[float, str]:
    """최근 short일 평균 / 최근 long일 평균. 오늘은 분자·분모 모두 제외(전일까지 완성값만).
    표본 부족(long일 미만) 시 1.0. 반환값: (multiplier, 진단문자열).
    """
    short = max(1, int(short))
    long = max(short, int(long))
    cache = _load_market_flow()
    keys = sorted([k for k in cache.keys() if k < today_key], reverse=True)
    if len(keys) < long:
        return 1.0, f"표본 {len(keys)}/{long}일 부족 → 배수 1.0"
    recent = [cache[k] for k in keys[:short]]
    baseline = [cache[k] for k in keys[:long]]
    r = sum(recent) / len(recent)
    b = sum(baseline) / len(baseline)
    if b <= 0:
        return 1.0, "기준값 0 → 배수 1.0"
    raw = r / b
    mult = max(float(low), min(float(high), raw))
    return mult, (f"최근{short}일 {r/1e8:,.0f}억 / 기준{long}일 {b/1e8:,.0f}억 "
                  f"= 원배수 {raw:.3f} → 적용 {mult:.3f}(클램프 {low}~{high})")


# ── 일봉 추세 평가 (관측 전용 · 선별/진입에 절대 영향 없음) ────────────────
def daily_trend_of(code: str) -> dict | None:
    """선별된 대장주의 '일봉 추세'를 평가해 상태만 기록한다(관측 전용).

    Minervini 트렌드템플릿 경량판: 정배열(종가≥MA20≥MA60[≥MA120]) + MA60 상방 +
    최근 60일 신고가 근처 여부. **선별(find_leaders)도 진입(leader_trader)도 이 값을
    읽지 않는다** — picks JSON·리포트에 라벨만 남겨, "우리가 뽑은 대장주가 실제로 일봉
    상승추세였나 / 추세가 나빴던 종목은 그날 실제로 덜 올랐나"를 관전에서 데이터로
    축적·사후 검증하기 위한 것. 우리 대장주는 장이 나빠도 뜨게 설계돼 있어, 일봉추세와
    실제 성과의 상관을 별도로 확인해야 한다(사용자 관찰).
    조회 실패/데이터 부족은 None(평가 생략) — 절대 선별·저장을 막지 않는다.

    반환: {trend: up|mixed|down, stacked, ma60_up, near_high60,
           close, ma20, ma60, ma120} 또는 None.
    """
    today = datetime.now().strftime("%Y%m%d")
    c = _TREND_CACHE.get(code)
    if c and c.get("date") == today:
        return c.get("val")
    val = None
    try:
        from pykrx import stock as krx
        end = datetime.now()
        start = end - timedelta(days=260)  # MA120 확보용 여유(휴장일 감안 ~170거래일)
        df = krx.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
        )
        if df is not None and not df.empty and "종가" in df.columns:
            closes = [float(x) for x in df["종가"].tolist() if x and x > 0]
            # 당일(마지막 행, 미완성) 제외 → 확정 일봉만
            hist = closes[:-1] if len(closes) > 1 else closes
            if len(hist) >= 20:
                def _sma(arr, n):
                    return (sum(arr[-n:]) / n) if len(arr) >= n else None
                last = hist[-1]
                ma20 = _sma(hist, 20)
                ma60 = _sma(hist, 60)
                ma120 = _sma(hist, 120)
                # 5거래일 전 MA60(= hist[-65:-5] 평균) 대비 상방인지 → 중기추세 방향
                ma60_prev = (sum(hist[-65:-5]) / 60) if len(hist) >= 65 else None
                ma60_up = bool(ma60 is not None and ma60_prev is not None
                               and ma60 >= ma60_prev)
                hi60 = max(hist[-60:]) if len(hist) >= 60 else max(hist)
                near_high60 = bool(hi60 > 0 and last >= hi60 * 0.85)
                stacked = bool(
                    ma20 is not None and ma60 is not None
                    and last >= ma20 >= ma60
                    and (ma120 is None or ma60 >= ma120)
                )
                if stacked and ma60_up:
                    trend = "up"
                elif ma60 is not None and last < ma60:
                    trend = "down"
                else:
                    trend = "mixed"
                val = {
                    "trend": trend, "stacked": stacked, "ma60_up": ma60_up,
                    "near_high60": near_high60, "close": round(last, 1),
                    "ma20": round(ma20, 1) if ma20 else None,
                    "ma60": round(ma60, 1) if ma60 else None,
                    "ma120": round(ma120, 1) if ma120 else None,
                }
    except Exception:
        val = None
    _TREND_CACHE[code] = {"date": today, "val": val}
    return val


# ── 2) 5일 평균 거래대금 (pykrx, 일 1회 캐시) ───────────────────────
def avg_value_5d(code: str) -> float:
    """최근 5거래일 평균 일중 거래대금(원).

    pykrx OHLCV 에 거래대금 컬럼이 없어 거래량×종가로 근사한다.
    조회 실패 시 최대 3회 리트라이하고, 그래도 실패하면 직전 거래일
    캐시값으로 폴백한다(평소 거래량은 하루로 거의 변하지 않음). 캐시도
    없으면 0.0.
    """
    today = datetime.now().strftime("%Y%m%d")
    c = _AVGVAL_CACHE.get(code)
    if c and c.get("date") == today and c.get("avg", 0) > 0:
        return float(c["avg"])
    avg = 0.0
    for attempt in range(3):  # pykrx 일시적 조회 실패 대비 리트라이
        try:
            from pykrx import stock as krx
            end = datetime.now()
            start = end - timedelta(days=21)
            df = krx.get_market_ohlcv_by_date(
                start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
            )
            if df is not None and not df.empty and {"거래량", "종가"} <= set(df.columns):
                val = (df["거래량"].astype(float) * df["종가"].astype(float))
                val = val[val > 0]
                if len(val) >= 2:
                    # 당일(마지막 행, 미완성) 제외 → 직전 최대 5거래일 평균
                    hist = val.iloc[:-1]
                    avg = float(hist.tail(5).mean()) if len(hist) >= 1 else 0.0
        except Exception:
            avg = 0.0
        if avg > 0:
            break
        if attempt < 2:
            time.sleep(0.4 * (attempt + 1))  # 점증 대기 후 재시도
    if avg > 0:
        _AVGVAL_CACHE[code] = {"date": today, "avg": avg}
        return avg
    # ── 폴백: 오늘 조회 실패 → 직전 거래일 캐시값 재사용 ──
    if c and c.get("avg", 0) > 0:
        return float(c["avg"])
    return 0.0


# ── 3) 섹터(네이버 업종) ─────────────────────────────────────────────
def _naver_group_name(upjong_no: str) -> str:
    if upjong_no in _GROUP_CACHE:
        return _GROUP_CACHE[upjong_no]
    name = ""
    try:
        url = (f"https://finance.naver.com/sise/sise_group_detail.naver"
               f"?type=upjong&no={upjong_no}")
        r = requests.get(url, headers=_HDR, timeout=10)
        r.encoding = "euc-kr"
        m = re.search(r"<title>\s*([^:<\n]+?)\s*(?::\s*Npay|</title>)", r.text)
        if m:
            name = m.group(1).strip()
    except Exception:
        pass
    _GROUP_CACHE[upjong_no] = name
    return name


def sector_of(code: str) -> str:
    if code in _SECTOR_CACHE:
        return _SECTOR_CACHE[code]
    sec = ""
    try:
        url = f"https://finance.naver.com/item/coinfo.naver?code={code}"
        r = requests.get(url, headers=_HDR, timeout=10)
        r.encoding = "euc-kr"
        m = re.search(r"upjong&no=(\d+)", r.text)
        if m:
            sec = _naver_group_name(m.group(1))
    except Exception:
        pass
    _SECTOR_CACHE[code] = sec or "(미상)"
    return _SECTOR_CACHE[code]


# ── 4) 네이버 테마 ──────────────────────────────────────────────────
_THEME_STOCK_CACHE: dict[str, set] = {}   # theme_no -> set(code)


def fetch_theme_list(min_change: float = -100.0) -> list[dict]:
    """네이버 핫테마 목록 반환 (전 페이지 크롤링).

    min_change : 테마 자체 등락률 하한(%). 기본 -100 → 사실상 비활성.
        테마 전체가 하락(-)이어도 그 안에 급등 종목이 숨어있을 수 있으므로
        (예: '반도체 장비' 테마등락 -2.5%인데 +3%↑ 종목 6개), 기본은 거르지 않고
        종목 상승률(rise_min·hot_min)로만 핫테마를 판정한다.

    반환: [{"no": "505", "name": "로봇", "change_pct": 6.83}, ...]
    """
    base = "https://finance.naver.com/sise/theme.naver"
    # 1페이지로 최대 페이지 수 파악
    try:
        r0 = requests.get(base, headers=_HDR, timeout=10)
        r0.encoding = "euc-kr"
    except Exception:
        return []
    max_page = max((int(p) for p in re.findall(r"page=(\d+)", r0.text)), default=1)

    results = []
    for pg in range(1, max_page + 1):
        if pg == 1:
            text = r0.text
        else:
            try:
                r = requests.get(f"{base}?&page={pg}", headers=_HDR, timeout=10)
                r.encoding = "euc-kr"
                text = r.text
            except Exception:
                continue

        # 테마번호+이름
        nos = re.findall(r"type=theme&no=(\d+)[^>]*>([^<]+)</a>", text)

        # 등락률 파싱 (등락/대비 컬럼이 있는 테이블)
        try:
            tables = pd.read_html(io.StringIO(text))
            tbl = None
            for t in tables:
                cols = [str(c) for c in t.columns]
                if any("등락" in c or "대비" in c for c in cols):
                    tbl = t
                    break
            if tbl is None:
                tbl = tables[0]
            # 두 번째 컬럼(전일대비 등락률) 파싱
            chg_col = tbl.iloc[:, 1].astype(str)
            chg_vals = chg_col.str.replace("%", "").str.replace("+", "").str.replace(",", "")
            chg_vals = pd.to_numeric(chg_vals, errors="coerce")
            # read_html 테이블엔 테마 사이 NaN 간격행이 섞여 있어 행 수가 테마명(정규식)
            # 보다 많다. NaN을 버리고 0부터 재색인해야 테마명 i ↔ 등락률 i 가 맞는다.
            # (예전엔 NaN 간격행 때문에 MLCC 등 ~100개 테마가 등락률=NaN으로 밀려 누락됐음)
            chg_vals = chg_vals.dropna().reset_index(drop=True)
        except Exception:
            continue

        for i, (no, name) in enumerate(nos):
            if i >= len(chg_vals):
                break
            chg = chg_vals.iloc[i] if i < len(chg_vals) else float("nan")
            if pd.isna(chg) or chg < min_change:
                continue
            results.append({"no": no, "name": name.strip(), "change_pct": float(chg)})

    results.sort(key=lambda x: x["change_pct"], reverse=True)
    return results


def fetch_theme_stocks(theme_no: str) -> set:
    """테마 상세 페이지에서 종목코드 집합 반환 (캐시).

    일시 실패 시 1회 재시도하고, 빈 결과는 캐시하지 않는다 — 165개 테마를
    연속 크롤링하다 한 페이지가 실패하면 빈 집합이 캐시돼 그 테마가 해당
    회차 핫섹터 판정에서 통째로 누락되는 문제가 있었음.
    """
    if theme_no in _THEME_STOCK_CACHE:
        return _THEME_STOCK_CACHE[theme_no]
    url = (f"https://finance.naver.com/sise/sise_group_detail.naver"
           f"?type=theme&no={theme_no}")
    codes: set = set()
    for _attempt in (1, 2):
        try:
            r = requests.get(url, headers=_HDR, timeout=10)
            r.encoding = "euc-kr"
            codes = set(re.findall(r"code=(\d{6})", r.text))
        except Exception:
            codes = set()
        if codes:
            break
        time.sleep(1)
    if codes:
        _THEME_STOCK_CACHE[theme_no] = codes
    return codes


# ── 점수 보조 함수 ─────────────────────────────────────────────────
def _pctile(values: list[float]) -> list[float]:
    """리스트를 0~1 백분위수로 변환(동순위=같은 값). n=1이면 [0.5](구버전 호환)."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    ranked = sorted(range(n), key=lambda i: values[i])
    result = [0.0] * n
    for rank, idx in enumerate(ranked):
        result[idx] = rank / (n - 1)
    return result


# ── n=1 절대값 정규화(강제 0.5 대체). 앵커는 대장주 실전값 기반 튜닝. ──
def _abs_log_value(v: float) -> float:
    """log10(거래대금원) 절대 정규화. 100억=0, 1조=0.67, 10조=1."""
    if v <= 0:
        return 0.0
    lv = math.log10(v)
    return max(0.0, min(1.0, (lv - 10.0) / 3.0))


def _abs_netbuy(v: float) -> float:
    """수급(주) 절대 정규화. 0=0.5 중립, +20만주=1.0, -20만주=0.0."""
    return max(0.0, min(1.0, 0.5 + v / 400000.0))


def _abs_change(v: float) -> float:
    """등락률 절대 정규화. 0%=0, 15%=1."""
    return max(0.0, min(1.0, v / 15.0))


def _abs_turnover(v: float) -> float:
    """회전율 절대 정규화. 0%=0, 10%=1."""
    return max(0.0, min(1.0, v / 10.0))


def _abs_vol_ratio(v: float) -> float:
    """평소대비 배수 절대 정규화. 1배=0, 5배=1."""
    return max(0.0, min(1.0, (v - 1.0) / 4.0))


def _compute_score_parts(vals, netbuy, chg, to, vr):
    """5개 pctile 리스트 반환. n>=2 는 상대순위, n==1 은 절대앵커.
    n==0 이면 모든 리스트가 빈 리스트."""
    n = len(vals)
    if n == 0:
        return [], [], [], [], []
    if n == 1:
        return ([_abs_log_value(vals[0])], [_abs_netbuy(netbuy[0])],
                [_abs_change(chg[0])], [_abs_turnover(to[0])],
                [_abs_vol_ratio(vr[0])])
    return (_pctile([math.log(max(v, 1)) for v in vals]),
            _pctile(netbuy), _pctile(chg), _pctile(to), _pctile(vr))


def _fetch_investor_flow(codes: list[str]) -> tuple[dict, bool, str]:
    """KIS 기관+외국인 수급 조회 — 당일 실시간 단일 소스.

    당일 실시간(FHKST01010100 UN)만 사용한다. 예전의 5일 히스토리 폴백(Tier2)은
    제거됨(사용자 결정) — 당일 수급을 못 받으면 억지로 과거값으로 때우지 않고,
    수급 가중치를 뺀 가중치(_stock_weights_nf)로만 선별한다.

    반환: (flow_dict, flow_ok, tier_label)
    - flow_ok=True  : 부분/완전 성공 → 수급 포함 가중치 사용(실패 종목은 0 처리)
    - flow_ok=False : 전 종목 실패 → 수급 가중치 제거 fallback
    - tier_label    : 로그·표시용 "T1"(수급O) / "T3"(수급X)

    디스코드 알림은 여기서 보내지 않는다 — 매 선별 시도마다 오는 스팸을 없애고,
    실제 선별 성공 알림(_summary_text)에 수급 요약만 1회 싣는다(사용자 결정).
    """
    try:
        from kis_quant import fetch_investor_netbuy
        from stock_bot.broker import KISBroker
        broker = KISBroker()
        try:
            raw = fetch_investor_netbuy(broker, codes)
            failed_codes = [c for c, v in raw.items() if v is None]
            ok_cnt = len(codes) - len(failed_codes)

            if ok_cnt == 0:
                # 전 종목 당일 수급 실패 → 수급 가중치 제거 fallback
                print(f"  [수급 T3] 당일 실시간 0/{len(codes)}건 → 수급 가중치 제거로 선별")
                return {}, False, "T3"

            # 부분/완전 성공 → 실패 종목은 0 처리하고 수급 가중치 사용.
            flow_dict = {k: (float(v) if v is not None else 0.0)
                         for k, v in raw.items()}
            pos = sum(1 for v in flow_dict.values() if v > 0)
            neg = sum(1 for v in flow_dict.values() if v < 0)
            print(f"  [수급 T1] 당일 실시간 {ok_cnt}/{len(codes)}건 성공"
                  + (f"(실패 {len(failed_codes)}건 0처리)" if failed_codes else "")
                  + f" · 순매수 {pos}종목 / 순매도 {neg}종목")
            return flow_dict, True, "T1"

        finally:
            broker.close()

    except Exception as e:
        print(f"  [수급 T3] 조회 예외 → 수급 가중치 제거로 선별: {e}")
        return {}, False, "T3"


def _normalize(weights: tuple[float, ...]) -> tuple[float, ...]:
    """가중치 합=1.0 으로 자동 정규화(요구사항 §3).

    사용자가 합 100%를 넘겨도(예: 50/70/40) 내부에서 비율만 유지해 점수를
    [0,1] 스케일로 유지한다. 합이 이미 1.0 이면 no-op(현행값 무변경). 음수는
    0으로 클램프하고, 총합 0(전부 0/음수)이면 원본 그대로 반환(0점 방지).
    """
    clamped = tuple(max(0.0, w) for w in weights)
    total = sum(clamped)
    if total <= 0:
        return weights
    return tuple(w / total for w in clamped)


def _stock_weights() -> tuple[float, float, float, float, float]:
    """종목 점수 가중치(log거래대금, 수급, 상승률, 회전율, 급증배율).

    settings 에서 읽어 파라미터탭 핫리로드를 반영한다. 실패 시 기본값 사용.
    합≠100% 입력도 _normalize 가 자동 정규화한다(§3).
    """
    try:
        from stock_bot.config.settings import settings as _s
        w = (
            float(getattr(_s, "lead_st_w_value",    0.35)),
            float(getattr(_s, "lead_st_w_flow",     0.20)),
            float(getattr(_s, "lead_st_w_updn",     0.15)),
            float(getattr(_s, "lead_st_w_turnover", 0.15)),
            float(getattr(_s, "lead_st_w_surge",    0.15)),
        )
    except Exception:
        w = (0.35, 0.20, 0.15, 0.15, 0.15)
    return _normalize(w)  # type: ignore[return-value]


def _stock_weights_nf() -> tuple[float, float, float, float, float]:
    """수급 조회 실패 시 fallback 가중치 — 수급 항목 제거(0), 나머지 재분배.

    기본: log거래대금 40% + 상승률 20% + 회전율 20% + 급증배율 20%.
    합≠100% 입력도 _normalize 가 자동 정규화한다(§3, 수급항목 0 유지).
    """
    try:
        from stock_bot.config.settings import settings as _s
        w = (
            float(getattr(_s, "lead_st_nf_w_value",    0.40)),
            0.0,
            float(getattr(_s, "lead_st_nf_w_updn",     0.20)),
            float(getattr(_s, "lead_st_nf_w_turnover", 0.20)),
            float(getattr(_s, "lead_st_nf_w_surge",    0.20)),
        )
    except Exception:
        w = (0.40, 0.0, 0.20, 0.20, 0.20)
    return _normalize(w)  # type: ignore[return-value]


def _sector_weights() -> tuple[float, float]:
    """섹터 점수 가중치(강도 intensity, 균등도 breadth).

    sector_score = mean(stock_scores) × (w_int + w_br × breadth)
    breadth = mean / max — 1종목 집중 시 ≈0, 고르게 상승 시 ≈1.
    intensity+breadth 를 합=1.0 으로 정규화(§3) → 배수 ∈ [intensity, 1].
    """
    try:
        from stock_bot.config.settings import settings as _s
        w = (
            float(getattr(_s, "lead_sc_w_intensity", 0.65)),
            float(getattr(_s, "lead_sc_w_breadth",   0.35)),
        )
    except Exception:
        w = (0.65, 0.35)
    return _normalize(w)  # type: ignore[return-value]


# ── 회전율(turnover %) 계산 헬퍼 ─────────────────────────────────────
# 분모: 유통주식수(상장주식수 근사, KIS 원본 · Naver 는 market_cap/price 로 역산).
# 시총 나눗셈보다 시장 실제 유통량 기준에 가까워 소형주 자동 편향을 완화한다.
def _turnover_pct_row(row) -> float:
    """행 단위 회전율(%) — 분자: 거래량, 분모: 유통주식수 근사.
    listed_shares 없으면 market_cap/price 로 역산. 어느 것도 없으면 0.
    """
    try:
        shares = float(row.get("listed_shares") or 0)
        if shares <= 0:
            mc = float(row.get("market_cap") or 0)
            px = float(row.get("price") or 0)
            shares = (mc / px) if (mc > 0 and px > 0) else 0
        vol = float(row.get("volume") or 0)
        if shares <= 0:
            # 최종 폴백: 거래량 부족 시 legacy 산식(거래대금/시총)
            v = float(row.get("value_won") or 0)
            mc = float(row.get("market_cap") or 0)
            return (v / mc * 100.0) if mc > 0 else 0.0
        return vol / shares * 100.0
    except (TypeError, ValueError):
        return 0.0


def _turnover_gate_min_pct(frac: float, base: float, slope: float) -> float:
    """시간대 계단 최저선(%) — 요구회전율 = base + slope × frac. 항상 활성."""
    return max(0.0, float(base) + float(slope) * float(max(0.0, min(1.0, frac))))


# ── 세션 경과 비율 ──────────────────────────────────────────────────
def _session_fraction(now: datetime | None = None) -> float:
    now = now or datetime.now()
    start = now.replace(hour=_SESSION_START[0], minute=_SESSION_START[1], second=0, microsecond=0)
    elapsed = (now - start).total_seconds() / 60.0
    frac = elapsed / _SESSION_MIN
    return min(max(frac, 0.02), 1.0)  # 너무 이른 시각엔 하한 2%


# ── 4) 대장주 선별 ──────────────────────────────────────────────────
def find_leaders_by_theme(rank_df: pd.DataFrame, vol_mult: float, frac: float,
                          min_value: float = 500e8, min_mktcap: float = 1000e8,
                          max_change: float = 29.5,
                          theme_min_change: float = -100.0,
                          rise_min: float = 3.0,
                          hot_min: int = 3,
                          turnover_gate_base: float = 1.0,
                          turnover_gate_slope: float = 15.0,
                          turnover_cap_pct: float = 200.0) -> dict:
    """테마 기반 대장주 선별.

    ① 거래대금 상위 rank_df (기존)
    ② 네이버 핫테마 목록 (등락률 theme_min_change% 이상)
    ③ 핫테마 ∩ rank_df 교집합에서 상승종목 hot_min개 이상인 테마만
    ④ 후보 중 거래대금·상승률 조건 통과한 상승률 1위 = 대장주
    """
    if rank_df.empty:
        return {"hot_sectors": [], "leaders": []}

    # 핫테마 가져오기
    hot_themes = fetch_theme_list(min_change=theme_min_change)
    if not hot_themes:
        return {"hot_sectors": [], "leaders": []}

    # ── Step 0: 자격 종목 사전 산정 — 5개 조건 모두 통과 ──
    #   등락 rise_min%↑ + 거래대금 min_value↑ + 시총 min_mktcap↑ + 평소대비 vol_mult배↑
    #   + (Level1 신규) 시간대 계단 회전율 하한(base>0 활성).
    #   핫섹터 강도(riser_count)와 대장주 후보 모두 '자격 종목'만으로 판정한다.
    to_gate_min = _turnover_gate_min_pct(frac, turnover_gate_base, turnover_gate_slope)
    qual_rows = []
    for _, row in rank_df.iterrows():
        if row["change_pct"] < rise_min:
            continue
        if row["value_won"] < min_value:
            continue
        if row.get("market_cap", 0) > 0 and row["market_cap"] < min_mktcap:
            continue
        avg5 = avg_value_5d(row["code"])
        expected = avg5 * frac if avg5 > 0 else 0.0
        ratio = row["value_won"] / expected if expected > 0 else 0.0
        if ratio < vol_mult:
            continue
        to_pct = _turnover_pct_row(row)
        if to_pct < to_gate_min:
            continue
        qual_rows.append({**row.to_dict(), "vol_ratio": ratio, "turnover_pct": to_pct})
    if not qual_rows:
        return {"hot_sectors": [], "leaders": []}
    qual_df = pd.DataFrame(qual_rows).reset_index(drop=True)

    # ── 수급 주입 및 종목 점수 계산 ──────────────────────────────────
    inv_flow, flow_ok, flow_tier = _fetch_investor_flow(qual_df["code"].tolist())
    qual_df["investor_netbuy"] = qual_df["code"].map(inv_flow).fillna(0.0)
    n_st = len(qual_df)
    if n_st > 0:
        _vals   = qual_df["value_won"].tolist()
        _netbuy = qual_df["investor_netbuy"].tolist()
        _chg    = qual_df["change_pct"].tolist()
        _vr     = qual_df["vol_ratio"].tolist()
        # 회전율(%) — 자격 필터에서 이미 계산해뒀으면 재사용, 없으면 즉시 계산.
        # 극단치 캡(turnover_cap_pct, 기본 200%) 로 pctile 왜곡 방지.
        if "turnover_pct" in qual_df.columns:
            _to_raw = qual_df["turnover_pct"].tolist()
        else:
            _to_raw = [_turnover_pct_row(qual_df.iloc[i]) for i in range(n_st)]
        _cap = float(turnover_cap_pct)
        _to = [min(x, _cap) if _cap > 0 else x for x in _to_raw]
        pc_lv, pc_nb, pc_chg, pc_to, pc_vr = _compute_score_parts(
            _vals, _netbuy, _chg, _to, _vr)
        _w = _stock_weights() if flow_ok else _stock_weights_nf()
        stock_scores: dict[str, float] = {}
        stock_score_parts: dict[str, dict] = {}
        for i in range(n_st):
            code = qual_df.at[i, "code"]
            stock_scores[code] = (
                pc_lv[i] * _w[0] + pc_nb[i] * _w[1] +
                pc_chg[i] * _w[2] + pc_to[i] * _w[3] + pc_vr[i] * _w[4]
            )
            stock_score_parts[code] = {
                "lv": round(pc_lv[i], 3), "nb": round(pc_nb[i], 3),
                "chg": round(pc_chg[i], 3), "to": round(pc_to[i], 3),
                "vr": round(pc_vr[i], 3),
                "mode": "abs" if n_st == 1 else "pctile",
            }
    else:
        stock_scores = {}
        stock_score_parts = {}

    # ── Step 1: 핫테마 후보 수집 (자격 종목 hot_min개↑ 테마만) ──────────
    OVERLAP_THR = 0.5   # 교집합/작은쪽 >= 50% 이면 같은 섹터로 간주
    # 지수성·광범위 테마 제외: 정부 밸류업 정책 묶음은 대형 상승주를 거의 다
    # 포함해 진짜 섹터(반도체 장비 등)를 가리는 노이즈이므로 후보에서 뺀다.
    THEME_EXCLUDE = ("밸류업", "value-up", "value up")
    theme_pool: list[dict] = []   # {"theme", "cands", "cand_codes", "riser_count"}

    for theme in hot_themes:
        name_l = theme["name"].lower()
        if any(x in name_l for x in THEME_EXCLUDE):
            continue
        t_codes = fetch_theme_stocks(theme["no"])
        # 핫섹터 강도: 자격 종목(4조건 통과) 중 이 테마 소속 수 (상한가 포함)
        sec_qual = qual_df[qual_df["code"].isin(t_codes)]
        riser_count = len(sec_qual)
        if riser_count < hot_min:
            continue
        # 대장주 후보: 자격 종목 중 상한가(max_change↑) 제외, 등락률 내림차순
        cands = sec_qual[sec_qual["change_pct"] < max_change].sort_values(
            "change_pct", ascending=False)
        # 핫섹터 구성 종목(상한가 포함) 등락률 내림차순 — 디스코드 표시용
        _mem = sec_qual.sort_values("change_pct", ascending=False)
        members = [{"name": r["name"], "change_pct": float(r["change_pct"])}
                   for _, r in _mem.iterrows()]
        total_val = float(sec_qual["value_won"].sum())
        total_mktcap = float(sec_qual["market_cap"].sum()) if "market_cap" in sec_qual.columns else 0.0
        avg_vr = float(sec_qual["vol_ratio"].mean())
        # 커플링: 대장(1위) vs 후발(2~3위) 동반 계수
        _sorted_chg = sec_qual["change_pct"].sort_values(ascending=False).tolist()
        top1_chg = _sorted_chg[0] if _sorted_chg else 0.0
        avg_foll = sum(_sorted_chg[1:3]) / len(_sorted_chg[1:3]) if len(_sorted_chg) > 1 else 0.0
        coupling = max(0.0, min(1.0, avg_foll / top1_chg)) if top1_chg > 0 else 0.0
        theme_pool.append({
            "theme": theme, "cands": cands,
            "cand_codes": set(cands["code"].tolist()),
            "qual_codes": set(sec_qual["code"].tolist()),
            "riser_count": riser_count,
            "total_value": total_val,
            "total_mktcap": total_mktcap,
            "avg_vol_ratio": avg_vr,
            "coupling": coupling,
            "avg_change": float(sec_qual["change_pct"].mean()),
            "members": members,
        })

    # ── Step 2: 겹치는 테마 병합 (상승종목 많은 테마 우선 유지) ─────────
    # 점수가 높은 테마를 대표로 남기기 위해 임시로 riser_count 기준 정렬(점수 계산 전).
    theme_pool.sort(key=lambda x: x["riser_count"], reverse=True)
    accepted: list[dict] = []
    accepted_codes: list[set] = []

    for item in theme_pool:
        c = item["cand_codes"]
        if not c:
            continue
        # 이미 선택된 테마 중 하나라도 50% 이상 겹치면 중복 테마로 스킵
        duplicate = False
        for ac in accepted_codes:
            inter = len(c & ac)
            smaller = min(len(c), len(ac))
            if smaller > 0 and inter / smaller >= OVERLAP_THR:
                duplicate = True
                break
        if not duplicate:
            accepted.append(item)
            accepted_codes.append(c)

    if not accepted:
        return {"hot_sectors": [], "leaders": []}

    # ── 테마 점수: mean(종목스코어) × (intensity + breadth × 균등도) ─────────────
    # breadth = mean/max — 1종목 집중 시 ≈0, 고르게 상승 시 ≈1.
    # 기본: 자격 전체 종목(qual_codes)의 stock_scores 사용.
    # §4-1 토글(leader_sel_sector_top3=ON): 상위 3종목만으로 강도·균등도를 계산.
    #   → 꼬리 종목이 평균을 끌어내리는 걸 막고, 1등이 2·3등을 압도하면 breadth가
    #     떨어져 자동으로 쏠림(imbalance) 페널티가 걸린다.
    try:
        from stock_bot.config.settings import settings as _s
        _top3 = bool(getattr(_s, "leader_sel_sector_top3", False))
    except Exception:
        _top3 = False
    w_int, w_br = _sector_weights()
    for a in accepted:
        sc_vals = [stock_scores[c] for c in a["qual_codes"] if c in stock_scores]
        if not sc_vals:
            a["sector_score"] = 0.0
            continue
        if _top3:
            sc_vals = sorted(sc_vals, reverse=True)[:3]
        s_mean = sum(sc_vals) / len(sc_vals)
        s_max  = max(sc_vals)
        breadth = s_mean / s_max if s_max > 0 else 1.0
        a["sector_score"] = round(s_mean * (w_int + w_br * breadth), 4)
    accepted.sort(key=lambda a: a["sector_score"], reverse=True)

    # ── Step 3: 대장주 선별 ─────────────────────────────────────────────
    hot_list = []
    leaders = []
    seen_codes: set = set()

    for item in accepted:
        theme = item["theme"]
        cands = item["cands"].copy()
        riser_count = item["riser_count"]
        sector_value = item["total_value"]
        sector_score = item["sector_score"]

        hot_list.append({
            "sector": theme["name"],
            "riser_count": riser_count,
            "total_value": sector_value,
            "avg_change": item["avg_change"],
            "sector_score": sector_score,
            "members": item.get("members", []),
        })

        # 종목 점수 기준으로 정렬 후 top3 바스켓 구성
        cands["_sc"] = cands["code"].map(stock_scores).fillna(0.0)
        cands = cands.sort_values("_sc", ascending=False)
        avail = [r for r in cands.to_dict("records") if r["code"] not in seen_codes]
        if not avail:
            continue
        lead_score = avail[0]["_sc"]
        top3 = []
        for k, r in enumerate(avail[:3]):
            top3.append({
                "rank": k + 1,
                "code": r["code"], "name": r["name"],
                "change_pct": round(float(r["change_pct"]), 2),
                "price": float(r["price"]),
                "value_won": float(r["value_won"]),
                "vol_ratio": round(float(r["vol_ratio"]), 2),
                "stock_score": round(float(r["_sc"]), 4),
                "score_parts": stock_score_parts.get(r["code"], {}),
                # 수급(기관+외인 순매수, 주). flow_ok=False면 0 → 표시측에서 '수급없음' 처리.
                "netbuy": float(r.get("investor_netbuy", 0) or 0),
            })
        row = avail[0]
        leaders.append({
            "sector": theme["name"],
            "code": row["code"], "name": row["name"],
            "change_pct": row["change_pct"], "price": row["price"],
            "value_won": row["value_won"], "vol_ratio": row["vol_ratio"],
            "sector_risers": riser_count,
            "sector_value": sector_value,
            "sector_score": sector_score,
            "theme_change": item["avg_change"],
            "stock_score": round(lead_score, 4),
            "score_parts": stock_score_parts.get(row["code"], {}),
            "netbuy": float(row.get("investor_netbuy", 0) or 0),
            "top3": top3,
        })
        _p = stock_score_parts.get(row["code"], {})
        if _p:
            _mode_kr = "상대순위" if _p.get("mode") == "pctile" else "절대점수(n=1)"
            _w_now = _stock_weights() if flow_ok else _stock_weights_nf()
            print(f"  [{row['code']} {row['name']}] 종합점수 {lead_score:.3f}  ({_mode_kr})")
            print(f"     거래대금 {_p.get('lv',0):.2f}×{_w_now[0]*100:.0f}%  |  "
                  f"수급 {_p.get('nb',0):.2f}×{_w_now[1]*100:.0f}%  |  "
                  f"등락률 {_p.get('chg',0):.2f}×{_w_now[2]*100:.0f}%  |  "
                  f"회전율 {_p.get('to',0):.2f}×{_w_now[3]*100:.0f}%  |  "
                  f"급증배수 {_p.get('vr',0):.2f}×{_w_now[4]*100:.0f}%")
        seen_codes.add(row["code"])

    # 대장주 순위: sector_score 기준(점수 시스템으로 통일)
    leaders.sort(key=lambda x: x.get("sector_score", 0), reverse=True)
    hot_list.sort(key=lambda x: x.get("sector_score", 0), reverse=True)
    # 수급 조회 상태 — 표시측(선별 알림·대시보드)에서 '수급O/수급없음' 배지에 사용.
    return {"hot_sectors": hot_list, "leaders": leaders,
            "flow_ok": bool(flow_ok), "flow_tier": flow_tier}


def find_leaders(rank_df: pd.DataFrame, rise_min: float, hot_min: int,
                 vol_mult: float, frac: float,
                 min_value: float = 500e8,
                 min_mktcap: float = 1000e8,
                 max_change: float = 29.5,
                 turnover_gate_base: float = 1.0,
                 turnover_gate_slope: float = 15.0,
                 turnover_cap_pct: float = 200.0) -> dict:
    """반환: {hot_sectors: [...], leaders: [...] }.

    min_value   : 거래대금 최소 절대값 (원). 기본 500억.
    min_mktcap  : 시가총액 최소 (원). 기본 1000억. market_cap=0이면 통과.
    max_change  : 등락률 상한 (%). 기본 29.5% → 상한가(30%) 제외.

    선별 순서:
      1) 거래대금 상위 종목 중 등락률 rise_min%↑ + 거래대금 500억↑ + 시총 1000억↑ + 평소대비 vol_mult배↑
      2) 조건 충족 종목이 같은 섹터에 hot_min개↑ → 핫섹터
      3) 핫섹터별 상승률 1위 (상한가 제외, 다음 순위로)
    """
    if rank_df.empty:
        return {"hot_sectors": [], "leaders": []}

    rank_df = rank_df.copy()
    rank_df["sector"] = rank_df["code"].map(sector_of)

    # 모든 조건 충족 종목 사전 산정 (avg_value_5d 캐시 활용)
    # + Level1 신규: 시간대 계단 회전율 하한(turnover_gate_base>0 활성)
    to_gate_min = _turnover_gate_min_pct(frac, turnover_gate_base, turnover_gate_slope)
    qualified_rows = []
    diag: dict = {
        "universe": int(len(rank_df)),
        "drops": {"rise": 0, "value": 0, "mktcap": 0, "vol_mult": 0, "turnover_gate": 0},
        "to_gate_min": float(to_gate_min),
        "rise_min": float(rise_min), "min_value": float(min_value),
        "min_mktcap": float(min_mktcap), "vol_mult": float(vol_mult),
        "near": [],  # 자격 탈락 상위(등락률순) — 어떤 필터에 걸렸는지
        "hot_min": int(hot_min),
        "sector_counts": {},
        "reason": "",
    }
    for _, row in rank_df.iterrows():
        _fail = None
        if row["change_pct"] < rise_min:
            diag["drops"]["rise"] += 1
            _fail = f"등락률 {float(row['change_pct']):+.2f}%<{rise_min:g}%"
        elif row["value_won"] < min_value:
            diag["drops"]["value"] += 1
            _fail = f"거래대금 {float(row['value_won'])/1e8:,.0f}억<{min_value/1e8:,.0f}억"
        elif row.get("market_cap", 0) > 0 and row["market_cap"] < min_mktcap:
            diag["drops"]["mktcap"] += 1
            _fail = f"시총 {float(row['market_cap'])/1e8:,.0f}억<{min_mktcap/1e8:,.0f}억"
        else:
            avg5 = avg_value_5d(row["code"])
            expected = avg5 * frac if avg5 > 0 else 0.0
            ratio = row["value_won"] / expected if expected > 0 else 0.0
            if ratio < vol_mult:
                diag["drops"]["vol_mult"] += 1
                _fail = f"평소대비 {ratio:.2f}배<{vol_mult:g}배"
            else:
                to_pct = _turnover_pct_row(row)
                if to_pct < to_gate_min:
                    diag["drops"]["turnover_gate"] += 1
                    _fail = f"회전율 {to_pct:.2f}%<{to_gate_min:.2f}%(게이트)"
                else:
                    qualified_rows.append({**row.to_dict(), "vol_ratio": ratio, "turnover_pct": to_pct})
                    continue
        # 자격 탈락 near-miss 기록 (등락률 상위만 유지)
        diag["near"].append({
            "code": str(row.get("code", "")),
            "name": str(row.get("name", "")),
            "sector": sector_of(row.get("code", "")),
            "change_pct": float(row.get("change_pct", 0) or 0),
            "value_eok": float(row.get("value_won", 0) or 0) / 1e8,
            "fail": _fail or "",
        })

    # near-miss 상위 8건만 (등락률순)
    diag["near"] = sorted(diag["near"], key=lambda x: x["change_pct"], reverse=True)[:8]

    if not qualified_rows:
        diag["reason"] = "자격 통과 종목 0"
        return {"hot_sectors": [], "leaders": [], "diag": diag}

    qual_df = pd.DataFrame(qualified_rows).reset_index(drop=True)

    # ── 수급(기관+외국인 순매수) 주입 ────────────────────────────────
    inv_flow, flow_ok, flow_tier = _fetch_investor_flow(qual_df["code"].tolist())
    qual_df["investor_netbuy"] = qual_df["code"].map(inv_flow).fillna(0.0)

    # ── 종목 점수 — 전체 자격종목 풀 기준 pctile ─────────────────────
    # 정상: log거래대금 35% + 수급 20% + 상승률 15% + 회전율 15% + 급증배율 15%
    # 수급실패: log거래대금 40% + 상승률 20% + 회전율 20% + 급증배율 20% (수급 0%)
    n_st = len(qual_df)
    if n_st > 0:
        _vals    = qual_df["value_won"].tolist()
        _netbuy  = qual_df["investor_netbuy"].tolist()
        _chg     = qual_df["change_pct"].tolist()
        _vr      = qual_df["vol_ratio"].tolist()
        # 회전율(%) — 자격 필터에서 이미 계산해뒀으면 재사용, 없으면 즉시 계산.
        # 극단치 캡(turnover_cap_pct, 기본 200%) 로 pctile 왜곡 방지.
        if "turnover_pct" in qual_df.columns:
            _to_raw = qual_df["turnover_pct"].tolist()
        else:
            _to_raw = [_turnover_pct_row(qual_df.iloc[i]) for i in range(n_st)]
        _cap = float(turnover_cap_pct)
        _to = [min(x, _cap) if _cap > 0 else x for x in _to_raw]
        pc_lv, pc_nb, pc_chg, pc_to, pc_vr = _compute_score_parts(
            _vals, _netbuy, _chg, _to, _vr)
        _w = _stock_weights() if flow_ok else _stock_weights_nf()
        stock_scores: dict[str, float] = {}
        stock_score_parts: dict[str, dict] = {}
        for i in range(n_st):
            code = qual_df.at[i, "code"]
            stock_scores[code] = (
                pc_lv[i] * _w[0] + pc_nb[i] * _w[1] +
                pc_chg[i] * _w[2] + pc_to[i] * _w[3] + pc_vr[i] * _w[4]
            )
            stock_score_parts[code] = {
                "lv": round(pc_lv[i], 3), "nb": round(pc_nb[i], 3),
                "chg": round(pc_chg[i], 3), "to": round(pc_to[i], 3),
                "vr": round(pc_vr[i], 3),
                "mode": "abs" if n_st == 1 else "pctile",
            }
    else:
        stock_scores = {}
        stock_score_parts = {}

    # 섹터별 자격종목 수 진단 기록
    for _sec_k, _sec_g in qual_df.groupby("sector"):
        if _sec_k in ("", "(미상)"):
            continue
        diag["sector_counts"][str(_sec_k)] = int(len(_sec_g))

    # 자격 통과 종목 + 종목점수 — 선별없음 시 "몇 점이었는지" 표시용
    diag["qualified"] = []
    for i in range(len(qual_df)):
        _code = qual_df.at[i, "code"]
        diag["qualified"].append({
            "code": str(_code),
            "name": str(qual_df.at[i, "name"]),
            "sector": str(qual_df.at[i, "sector"]),
            "change_pct": float(qual_df.at[i, "change_pct"]),
            "value_eok": float(qual_df.at[i, "value_won"]) / 1e8,
            "vol_ratio": float(qual_df.at[i, "vol_ratio"]),
            "turnover_pct": float(qual_df.at[i, "turnover_pct"]),
            "stock_score": float(stock_scores.get(_code, 0.0)),
            "parts": stock_score_parts.get(_code, {}),
        })
    diag["qualified"].sort(key=lambda x: x["stock_score"], reverse=True)

    # ── 섹터별 집계 — pctile 섹터 점수 계산 ─────────────────────────
    sec_raw: list[dict] = []
    for sec, g in qual_df.groupby("sector"):
        if sec in ("", "(미상)"):
            continue
        if len(g) < hot_min:
            continue
        g_sorted = g.sort_values("change_pct", ascending=False)
        top1_chg = float(g_sorted.iloc[0]["change_pct"])
        followers_chg = g_sorted.iloc[1:3]["change_pct"].tolist()
        avg_foll = sum(followers_chg) / len(followers_chg) if followers_chg else 0.0
        coupling = max(0.0, min(1.0, avg_foll / top1_chg)) if top1_chg > 0 else 0.0
        total_mktcap = float(g["market_cap"].sum()) if "market_cap" in g.columns else 0.0
        total_value  = float(g["value_won"].sum())
        avg_change   = float(g["change_pct"].mean())
        avg_vr       = float(g["vol_ratio"].mean())
        turnover     = total_value / total_mktcap if total_mktcap > 0 else 0.0
        _mem = g_sorted
        members = [{"name": r["name"], "change_pct": float(r["change_pct"])}
                   for _, r in _mem.iterrows()]
        sec_raw.append({
            "sector": sec, "riser_count": len(g),
            "total_value": total_value, "total_mktcap": total_mktcap,
            "avg_change": avg_change, "avg_vol_ratio": avg_vr,
            "turnover": turnover, "coupling": coupling,
            "members": members,
        })

    if not sec_raw:
        diag["reason"] = f"자격 {len(qual_df)}종목 통과했지만 hot_min({hot_min})↑ 섹터 없음"
        return {"hot_sectors": [], "leaders": [], "diag": diag}

    # 섹터 점수 = pctile(상승률)×0.25 + pctile(회전율)×0.25 + pctile(log거래대금)×0.25
    #            + pctile(급증배율)×0.25, × 커플링 C
    pc_sc  = _pctile([s["avg_change"] for s in sec_raw])
    pc_sto = _pctile([s["turnover"] for s in sec_raw])
    pc_slv = _pctile([math.log(max(s["total_value"], 1)) for s in sec_raw])
    pc_svr = _pctile([s["avg_vol_ratio"] for s in sec_raw])
    for i, s in enumerate(sec_raw):
        raw_sc = (pc_sc[i] + pc_sto[i] + pc_slv[i] + pc_svr[i]) / 4
        s["sector_score"] = round(raw_sc * s["coupling"], 4)

    hot = sorted(sec_raw, key=lambda s: s["sector_score"], reverse=True)

    # ── 대장주: 핫섹터별 종목점수 1위 (상한가 제외), top3 바스켓 ────────
    leaders = []
    for s in hot:
        sec = s["sector"]
        cands = qual_df[
            (qual_df["sector"] == sec) & (qual_df["change_pct"] < max_change)
        ].copy()
        cands["_sc"] = cands["code"].map(stock_scores).fillna(0.0)
        cands = cands.sort_values("_sc", ascending=False)
        if cands.empty:
            continue
        avail = cands.to_dict("records")
        if not avail:
            continue
        lead_score = avail[0]["_sc"]
        top3 = []
        for k, r in enumerate(avail[:3]):
            top3.append({
                "rank": k + 1,
                "code": r["code"], "name": r["name"],
                "change_pct": round(float(r["change_pct"]), 2),
                "price": float(r["price"]),
                "value_won": float(r["value_won"]),
                "vol_ratio": round(float(r["vol_ratio"]), 2),
                "stock_score": round(float(r["_sc"]), 4),
                "score_parts": stock_score_parts.get(r["code"], {}),
                # 수급(기관+외인 순매수, 주). flow_ok=False면 0 → 표시측에서 '수급없음' 처리.
                "netbuy": float(r.get("investor_netbuy", 0) or 0),
            })
        row = avail[0]
        leaders.append({
            "sector": sec,
            "code": row["code"], "name": row["name"],
            "change_pct": row["change_pct"], "price": row["price"],
            "value_won": row["value_won"], "vol_ratio": row["vol_ratio"],
            "sector_risers": s["riser_count"],
            "sector_value": s["total_value"],
            "sector_score": s["sector_score"],
            "stock_score": round(lead_score, 4),
            "score_parts": stock_score_parts.get(row["code"], {}),
            "top3": top3,
        })
        _p = stock_score_parts.get(row["code"], {})
        if _p:
            _mode_kr = "상대순위" if _p.get("mode") == "pctile" else "절대점수(n=1)"
            _w_now = _stock_weights() if flow_ok else _stock_weights_nf()
            print(f"  [{row['code']} {row['name']}] 종합점수 {lead_score:.3f}  ({_mode_kr})")
            print(f"     거래대금 {_p.get('lv',0):.2f}×{_w_now[0]*100:.0f}%  |  "
                  f"수급 {_p.get('nb',0):.2f}×{_w_now[1]*100:.0f}%  |  "
                  f"등락률 {_p.get('chg',0):.2f}×{_w_now[2]*100:.0f}%  |  "
                  f"회전율 {_p.get('to',0):.2f}×{_w_now[3]*100:.0f}%  |  "
                  f"급증배수 {_p.get('vr',0):.2f}×{_w_now[4]*100:.0f}%")
    if not leaders:
        diag["reason"] = f"핫섹터 {len(hot)}개 있지만 대장주 후보 없음(과열컷 {max_change:g}%초과 배제 등)"
    return {"hot_sectors": hot, "leaders": leaders, "diag": diag}


# ── 리포트 출력 ─────────────────────────────────────────────────────
def _report(rank_df: pd.DataFrame, res: dict, args, frac: float,
            when: datetime | None = None) -> None:
    now = (when or datetime.now()).strftime("%H:%M:%S")
    print(f"\n{'='*96}")
    print(f"[{now}] 09:00~현재 코스피+코스닥 통합 상위 {len(rank_df)}종목(보통주) | 세션경과 {frac*100:.0f}% | "
          f"상승기준 +{args.rise_min:g}% | 거래대금 {args.vol_mult:g}배·{args.min_value:.0f}억↑ | "
          f"시총 {args.min_mktcap:.0f}억↑ | 과열주({args.max_change:g}%↑) 제외")
    print("=" * 96)

    hot = res["hot_sectors"]
    if hot:
        print(f"\n■ 주도(핫) 섹터  (상승종목 {args.hot_min}개+ , 섹터강도순=상승종목수→거래대금)")
        print(f"{'섹터':<20} {'상승종목수':>8} {'거래대금합(억)':>14} {'평균등락':>8}")
        print("-" * 56)
        for s in hot[:8]:
            print(f"{s['sector']:<20} {s['riser_count']:>8} "
                  f"{s['total_value']/1e8:>13,.0f} {s['avg_change']:>+7.2f}%")
    else:
        print("\n  핫섹터 없음 (상승 종목이 섹터별로 충분치 않음)")

    _flow_ok = bool(res.get("flow_ok", False))
    print(f"\n■ 수급: {'💰 당일 실시간 반영' if _flow_ok else '⚠️ 미도달 → 수급 가중치 제거로 선별'}"
          + (f" (tier {res.get('flow_tier','')})" if res.get('flow_tier') else ""))

    leaders = res["leaders"]
    print("\n■ 대장주 후보  (섹터강도순=상승종목수→거래대금, 섹터내 상승률 1위)")
    if leaders:
        print(f"{'섹터':<18} {'종목':<16} {'현재가':>9} {'등락률':>8} "
              f"{'거래대금(억)':>12} {'평소대비':>8} {'섹터상승':>7}")
        print("-" * 88)
        for L in leaders:
            print(f"{L['sector']:<18} {L['name'][:14]:<16} {L['price']:>9,.0f} "
                  f"{L['change_pct']:>+7.2f}% {L['value_won']/1e8:>11,.0f} "
                  f"{L['vol_ratio']:>6.1f}x {L.get('sector_risers', 0):>6}개")
            # 점수·수급(관측 전용) — 섹터점수/종목점수 및 당일 순매수(수급없음이면 생략)
            _nb = float(L.get("netbuy", 0) or 0)
            _nb_s = (f" · 수급 {_nb/1e4:+,.0f}만주" if _flow_ok and abs(_nb) >= 1e4
                     else (f" · 수급 {_nb:+,.0f}주" if _flow_ok else ""))
            print(f"{'':<18}   └ 점수: 섹터 {float(L.get('sector_score',0) or 0):.3f}"
                  f" · 종목 {float(L.get('stock_score',0) or 0):.3f}{_nb_s}")
            # 일봉추세(관측 전용 · 선별/진입에 영향 없음) — 라벨만 참고 출력
            _dt = daily_trend_of(L["code"])
            if _dt:
                _flags = []
                if _dt.get("stacked"):     _flags.append("정배열")
                if _dt.get("ma60_up"):     _flags.append("MA60↑")
                if _dt.get("near_high60"): _flags.append("60일고가권")
                print(f"{'':<18}   └ 일봉추세: {_dt.get('trend','?'):<5}"
                      f" ({', '.join(_flags) if _flags else '해당없음'})"
                      f"  MA20 {_dt.get('ma20')} · MA60 {_dt.get('ma60')}"
                      f" · MA120 {_dt.get('ma120')}")
            else:
                print(f"{'':<18}   └ 일봉추세: (조회 생략)")
    else:
        print("  조건 충족 대장주 없음")
        _dg = res.get("diag") or {}
        if _dg:
            _dr = _dg.get("drops", {})
            _qc = sum(_dg.get("sector_counts", {}).values())
            print(f"    · 유니버스 {_dg.get('universe',0)}종목 → 자격통과 {_qc}종목 "
                  f"(회전율게이트 하한 {_dg.get('to_gate_min',0):.2f}%, "
                  f"조건 rise≥{_dg.get('rise_min',0):g}%·거래대금≥{_dg.get('min_value',0)/1e8:,.0f}억·"
                  f"시총≥{_dg.get('min_mktcap',0)/1e8:,.0f}억·평소×{_dg.get('vol_mult',0):g})")
            print(f"    · 탈락 사유별: 등락{_dr.get('rise',0)} · 거래대금{_dr.get('value',0)} · "
                  f"시총{_dr.get('mktcap',0)} · 평소대비{_dr.get('vol_mult',0)} · "
                  f"회전율게이트{_dr.get('turnover_gate',0)}")
            _sc = _dg.get("sector_counts") or {}
            if _sc:
                _top = sorted(_sc.items(), key=lambda kv: kv[1], reverse=True)[:5]
                _s = ", ".join(f"{k}({v})" for k, v in _top)
                print(f"    · 자격 섹터분포(hot_min={_dg.get('hot_min',0)}↑ 필요): {_s}")
            _ql = _dg.get("qualified") or []
            if _ql:
                print(f"    · 자격통과 종목 · 종목점수 순 (선별 직전 상태, 총 {len(_ql)}):")
                for _q in _ql[:8]:
                    _p = _q.get("parts") or {}
                    _mode = "abs" if _p.get("mode") == "abs" else "pct"
                    print(f"        [{_q['code']} {_q['name'][:12]:<12}] "
                          f"점수 {_q['stock_score']:.3f}({_mode}) · "
                          f"{_q['change_pct']:+6.2f}% · {_q['value_eok']:>6,.0f}억 · "
                          f"회전 {_q['turnover_pct']:>5.2f}% · 평소{_q['vol_ratio']:>4.2f}x · "
                          f"[{_q['sector']}]")
            _nr = _dg.get("near") or []
            if _nr:
                print(f"    · 아깝게 탈락 (등락률순, 자격 불충족 사유):")
                for _n in _nr[:5]:
                    print(f"        [{_n['code']} {_n['name'][:12]:<12}] "
                          f"{_n['change_pct']:+6.2f}% · {_n['value_eok']:>6,.0f}억 · "
                          f"[{_n['sector']}] ← {_n['fail']}")
            if _dg.get("reason"):
                print(f"    · 최종: {_dg['reason']}")
    print()


def _b6(code) -> str:
    """종목코드 정규화(.KS/.KQ 접미사 제거) — own/바스켓 비교용."""
    return str(code or "").split(".")[0].strip()


def _read_overrides_kv(keys: tuple[str, ...]) -> dict[str, str]:
    """.env → .env.overrides 순서로 파싱해 지정 키만 반환 (overrides 우선).

    이 프로세스는 leader_runner 가 매 선별마다 새로 띄우는 subprocess 다. 도커는
    컨테이너 시작 시 env 를 os.environ 에 고정하므로, os.environ/pydantic 스냅샷으로
    읽으면 스크리너가 로테이션한 최신 SYMBOLS 를 못 본다(컨테이너 재시작 전까지 stale).
    스크리너가 실제로 갱신하는 파일(.env.overrides)을 직접 읽어 최신값을 쓴다.
    """
    out: dict[str, str] = {}
    for fn in (".env", ".env.overrides"):  # overrides 가 뒤 → 같은 키 덮어씀(우선)
        try:
            for raw in (HERE / fn).read_text(encoding="utf-8").splitlines():
                s = raw.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                if k not in keys:
                    continue
                v = v.strip().split("#", 1)[0].strip()  # 인라인 주석 제거
                if v and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                out[k] = v
        except OSError:
            continue
    return out


def _basket_rule_params() -> tuple[float, set[str], bool]:
    """매매 바스켓 룰 파라미터(top3_ratio, 스톡봇 보유종목, own-symbol 우선권)
    — leader_trader 와 동일 기준.

    SYMBOLS/토글은 스크리너가 .env.overrides 를 갱신하므로 파일에서 직접 읽어
    최신 로테이션을 반영한다. 파일에 없으면 settings 폴백(개발/독립 실행).
    """
    kv = _read_overrides_kv(
        ("SYMBOLS", "TRADE_SYMBOLS", "LEADER_TOP3_RATIO", "LEADER_OWN_SYMBOL_PRIORITY"))
    try:
        ratio = float(kv.get("LEADER_TOP3_RATIO", "0.6") or 0.6)
    except Exception:
        ratio = 0.6
    prio = kv.get("LEADER_OWN_SYMBOL_PRIORITY", "").lower() in ("1", "true", "yes", "on")
    raw = kv.get("SYMBOLS") or kv.get("TRADE_SYMBOLS") or ""
    own = {_b6(s) for s in raw.split(",") if s.strip()}
    if not own or "LEADER_OWN_SYMBOL_PRIORITY" not in kv:
        # 파일에서 못 읽은 값만 settings 로 보완 (SYMBOLS 부재·토글 부재 시)
        try:
            from stock_bot.config.settings import settings as _s
            if not own:
                own = {_b6(s) for s in _s.symbols}
            if "LEADER_OWN_SYMBOL_PRIORITY" not in kv:
                prio = bool(_s.leader_own_symbol_priority)
        except Exception:
            pass
    return ratio, own, prio


def _summary_text(res: dict, args, frac: float,
                  when: datetime | None = None) -> str:
    """대장주 선별 결과를 디스코드/웹 공용 요약 텍스트로 생성."""
    now = (when or datetime.now()).strftime("%H:%M")
    leaders = res.get("leaders", [])
    hot = res.get("hot_sectors", [])

    mode_tag = "🗂️테마" if getattr(args, "theme", False) else "🏭업종"
    # 수급 상태 배지 — 매 조회 스팸 대신 선별 성공 알림에 1회만 싣는다(사용자 결정).
    flow_ok = bool(res.get("flow_ok", False))
    flow_badge = "💰수급O" if flow_ok else "⚠️수급없음(가중치제거)"

    def _fmt_nb(v: float) -> str:
        """순매수 수량(주) → 읽기 쉬운 만주 단위. 수급없음이면 '—'."""
        if not flow_ok:
            return "—"
        man = v / 1e4
        return f"{man:+,.0f}만주" if abs(man) >= 1 else f"{v:+,.0f}주"

    lines = [f"**📊 대장주 선별 [{now}] [{mode_tag}]** | 세션경과 {frac*100:.0f}% | {flow_badge}"]

    if leaders:
        lines.append("")
        lines.append("**🏆 대장주 후보** (섹터점수 순)")
        for i, L in enumerate(leaders, 1):
            lines.append(
                f"`{i}위` **{L['name']}** ({L['code']})  "
                f"{L['change_pct']:+.1f}%  "
                f"거래대금 {L['value_won']/1e8:.0f}억  "
                f"평소대비 {L['vol_ratio']:.1f}x  "
                f"수급 {_fmt_nb(float(L.get('netbuy', 0) or 0))}"
            )
            lines.append(
                f"　　　섹터: {L['sector']} · 섹터점수 {float(L.get('sector_score', 0) or 0):.3f} "
                f"· 종목점수 {float(L.get('stock_score', 0) or 0):.3f} "
                f"(상승 {L.get('sector_risers', 0)}종목)"
            )

        # 매매 바스켓 — 실제 매매봇이 1등 섹터 top3에 적용하는 룰 그대로 미리보기.
        # 왜 일부 후보가 빠지는지(예: 스톡봇 보유종목·비율 미달) 알림에서 바로 확인.
        # own-symbol 우선권(점유락)이 켜져 있으면 스톡봇과 겹쳐도 제외하지 않고,
        # 먼저 잡는 봇이 가져간다 → leader_trader.py 판정과 동일하게 표시.
        ratio, own, own_priority = _basket_rule_params()
        top3 = sorted((leaders[0].get("top3") or []), key=lambda x: x.get("rank", 9))
        if top3:
            # 점수 기반 바스켓 룰: 2·3등의 stock_score가 1등의 ratio% 이상이어야 포함.
            # stock_score 없는 구버전 picks 호환: change_pct 기반 폴백.
            lead_sc = float(top3[0].get("stock_score", 0))
            lead_chg = float(top3[0].get("change_pct", 0))
            use_score = lead_sc > 0
            thresh_sc = lead_sc * ratio
            thresh_chg = lead_chg * ratio
            lines.append("")
            _own_desc = "겹침=점유락(먼저 잡는 봇)" if own_priority else "스톡봇 종목 제외"
            lines.append(f"**🧮 매매 바스켓** ({ratio*100:.0f}% 룰 · {_own_desc})")
            for m in top3:
                code = _b6(m.get("code"))
                chg = float(m.get("change_pct", 0))
                sc = float(m.get("stock_score", 0))
                nm = m.get("name", "")
                if m.get("rank", 1) >= 2:
                    below = (sc < thresh_sc) if use_score else (chg < thresh_chg)
                    if below:
                        _base = f"점수 {sc:.3f} (기준 {thresh_sc:.3f})" if use_score else f"{chg:+.1f}% (기준 {thresh_chg:+.1f}%)"
                        lines.append(f"　❌ {nm}({code}) {chg:+.1f}% — {ratio*100:.0f}%룰 미달({_base})")
                        continue
                if code in own:
                    if own_priority:
                        lines.append(f"　⚖️ {nm}({code}) {chg:+.1f}% — 스톡봇과 겹침(점유락: 먼저 잡는 봇)")
                    else:
                        lines.append(f"　❌ {nm}({code}) {chg:+.1f}% — 스톡봇 보유종목")
                else:
                    sc_tag = f" [점수:{sc:.3f}]" if use_score else ""
                    lines.append(f"　✅ {nm}({code}) {chg:+.1f}%{sc_tag}")
    else:
        lines.append("⚠️ 조건 충족 대장주 없음")

    if hot:
        lines.append("")
        lines.append("**🔥 핫섹터**")
        for s in hot[:6]:
            lines.append(
                f"• {s['sector']}  상승종목 {s['riser_count']}개  "
                f"평균 {s['avg_change']:+.1f}%  "
                f"{s['total_value']/1e8:.0f}억"
            )
            members = s.get("members", [])
            if members:
                mem_str = ", ".join(
                    f"{m['name']}({m['change_pct']:+.1f}%)" for m in members[:10]
                )
                lines.append(f"　　{mem_str}")

    return "\n".join(lines)


def _discord_notify(res: dict, args, frac: float,
                    when: datetime | None = None) -> None:
    """대장주 선별 결과를 디스코드로 전송.

    미선별(leaders 비어있음)인데 --suppress-empty-alert 면 전송 생략 —
    10분 간격 재시도마다 '없음'을 쏘는 스팸을 막고, 마지막 시도(러너가 플래그
    미부착)에서만 미선별 알림이 1회 나가게 한다. 선별 성공은 항상 전송.
    """
    if not res.get("leaders") and getattr(args, "suppress_empty_alert", False):
        return
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    msg = _summary_text(res, args, frac, when)
    try:
        r = requests.post(url, json={"content": msg, "username": "대장주알림"}, timeout=10)
        if not (200 <= r.status_code < 300):
            print(f"  [디스코드 전송 실패] HTTP {r.status_code}")
    except Exception as e:
        print(f"  [디스코드 전송 실패] {e}")


# 임시공휴일(선거일 등) 추가 휴장일. 공유 모듈을 우선 사용하되, 독립 실행
# (stock_bot 미임포트) 상황에서도 동작하도록 자체 상수로 폴백한다.
_EXTRA_HOLIDAYS = {
    "2026-06-03",  # 제9회 전국동시지방선거 (임시공휴일)
}
try:
    from stock_bot.market_calendar import get_extra_holidays as _shared_extra_holidays
except Exception:
    _shared_extra_holidays = None


def _is_trading_day(date: datetime | None = None) -> bool:
    """KRX 거래일 여부 — 공휴일·대체공휴일·임시공휴일 포함 체크. 실패 시 True(실행 허용).

    leader_finder 는 KIS 미사용이라 KIS 휴장일 API를 못 쓴다.
    공유 모듈(get_extra_holidays, 웹 등록분 포함)을 우선 쓰고, 임포트 실패 시
    자체 _EXTRA_HOLIDAYS 로 폴백한다. 정규공휴일은 exchange_calendars 로 커버.
    """
    date = date or datetime.now()
    if date.weekday() >= 5:
        return False
    extra = _shared_extra_holidays() if _shared_extra_holidays else _EXTRA_HOLIDAYS
    if date.strftime("%Y-%m-%d") in extra:
        return False
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XKRX")
        return bool(cal.is_session(date.strftime("%Y-%m-%d")))
    except Exception:
        return True  # 확인 실패 시 실행 허용


def _is_market_hours() -> bool:
    now = datetime.now()
    if not _is_trading_day(now):
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60) <= hm <= (15 * 60 + 30)


_PICKS_DIR = _CACHE_DIR / "leader_picks"


def _save_picks(res: dict, args, frac: float,
                when: datetime | None = None) -> None:
    """선별된 대장주를 날짜별 JSON으로 적재(전진검증용). 다음날 점수화에 사용."""
    leaders = res.get("leaders", [])
    if not leaders:
        return
    _PICKS_DIR.mkdir(parents=True, exist_ok=True)
    now = when or datetime.now()
    # --reval(섹터 전환용 재선별)은 정본 <날짜>.json 을 덮지 않고 _reval 로 분리 저장.
    suffix = "_reval" if getattr(args, "reval", False) else ""
    path = _PICKS_DIR / f"{now:%Y-%m-%d}{suffix}.json"
    payload = {
        "date": now.strftime("%Y-%m-%d"),
        "selected_at": now.strftime("%H:%M:%S"),
        "session_fraction": round(frac, 4),
        "params": {"rise_min": args.rise_min, "hot_min": args.hot_min,
                   "vol_mult": args.vol_mult, "top": args.top},
        # 수급 조회 상태 — 대시보드·알림 '수급O/수급없음' 배지용. leaders[*].netbuy 는
        # 종목별 순매수(주). flow_ok=False면 수급 가중치 제거로 선별된 것.
        "flow_ok": bool(res.get("flow_ok", False)),
        "flow_tier": res.get("flow_tier", ""),
        "leaders": [
            {"code": L["code"], "name": L["name"], "sector": L["sector"],
             "change_pct": round(float(L["change_pct"]), 2),
             "price": float(L["price"]),
             "value_won": float(L["value_won"]),
             "vol_ratio": round(float(L["vol_ratio"]), 2),
             # 섹터 강도(상승종목 수) — 섹터 전환 히스테리시스 판정용. 정렬 키와 동일.
             "sector_risers": int(L.get("sector_risers", 0) or 0),
             # 섹터/종목 점수 — 섹터 전환 점수 판정용(leader_trader._maybe_switch).
             # 이 값이 빠져 있으면 전환 로직이 sector_score=0 으로 읽어 점수 기반
             # 교체·축출이 전부 무력화된다(슬롯 포화 시 0≤0 → 영구 '추가 불가').
             "sector_score": round(float(L.get("sector_score", 0) or 0), 4),
             "stock_score": round(float(L.get("stock_score", 0) or 0), 4),
             # 대장주 본인 수급(기관+외인 순매수, 주). 수급 실패 시 0.
             "netbuy": float(L.get("netbuy", 0) or 0),
             # 탑3 바스켓 검증용: 섹터 내 자격종목 상승률 1·2·3등 (1등=대장주 본인)
             "top3": L.get("top3", []),
             # 일봉추세 라벨(관측 전용 · 선별/진입에 영향 없음). 사후 검증용 —
             # 추세 나쁜 종목이 그날 실제로 덜 올랐는지 상관 확인. 조회실패 시 None.
             "daily_trend": daily_trend_of(L["code"])}
            for L in leaders
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    if not getattr(args, "summary_only", False):
        print(f"  → 선별 결과 저장: {path.relative_to(HERE)} ({len(leaders)}종목)")


def run_once(args) -> None:
    start_dt = datetime.now()  # 선별 시작(=크론 발화) 시각 — 표시 시각 기준
    frac = _session_fraction(start_dt)
    # 네이버 KRX+NXT 통합(1순위) — 크롤링 4건(시장×거래소)으로 KIS 유량 완전 회피.
    # 3분 주기 reval 대응. KIS 유량 부담 크고 UN 재조회 α건이 지연 유발.
    try:
        rank_df = fetch_ranking_unified(top_n=args.top, stock_only=not args.include_etf)
    except Exception as e:
        print(f"  [네이버 통합 실패 → KIS 폴백] {e}")
        rank_df = pd.DataFrame()
    if rank_df is None or rank_df.empty:
        # 1차 폴백: KIS 통합(KRX+NXT) — 브로커 API, 유량 소모 있음.
        print("  [폴백] 네이버 통합 빈 결과 → KIS 통합 거래대금 사용")
        try:
            rank_df = kis_quant.fetch_ranking(
                top_n=args.top, stock_only=not args.include_etf,
                min_value=args.min_value * 1e8)
        except Exception as e:
            print(f"  [KIS 실패 → 네이버 KRX 단독 폴백] {e}")
            rank_df = pd.DataFrame()
        # 2차 폴백: KIS도 실패면 네이버 KRX 단독(NXT 누락 위험) blackout 방지.
        if rank_df is None or rank_df.empty:
            print("  [폴백2] KIS 빈 결과 → 네이버 KRX 단독(sise_quant)")
            rank_df = fetch_ranking(top_n=args.top, stock_only=not args.include_etf)
    if rank_df.empty:
        print("  [경고] 순위 데이터 수집 실패")
        return
    # ── §2 동적 거래대금 임계값(토글, 기본 OFF) ──────────────────────────────
    # dyn_value_pct>0 이면 고정 min_value(억) 대신 '유니버스(코스피+코스닥 통합
    # 상위) 거래대금 합 × pct%'를 종목별 거래대금 하한으로 사용 → 장중 활황도에
    # 비례해 자동 조정. 0(기본)이면 고정값 그대로 → 동작 불변. 다른 게이트·선별
    # 로직은 무변경, 거래대금 하한값만 대체한다.
    eff_min_value = args.min_value * 1e8
    dyn_pct = float(getattr(args, "dyn_value_pct", 0.0) or 0.0)
    # 오늘 유니버스 합은 항상 캐시에 기록(다음날 market_flow 분자·분모 표본용).
    today_base_sum = float(rank_df["value_won"].sum())
    today_key = start_dt.strftime("%Y%m%d")
    _record_market_flow(today_key, today_base_sum)
    if dyn_pct > 0:
        # legacy §2 방식(오늘 유니버스합 × pct%) — market_flow 보다 우선.
        eff_min_value = today_base_sum * dyn_pct / 100.0
        print(f"  [동적 거래대금·legacy] 유니버스합 {today_base_sum/1e8:,.0f}억 × {dyn_pct:g}% "
              f"= {eff_min_value/1e8:,.0f}억 (고정 {args.min_value:.0f}억 대체)")
    else:
        mf_short = int(getattr(args, "mf_window_short", 5))
        mf_long  = int(getattr(args, "mf_window_long", 20))
        mf_low   = float(getattr(args, "mf_clamp_low", 0.5))
        mf_high  = float(getattr(args, "mf_clamp_high", 2.0))
        mult, diag = _compute_market_flow_multiplier(today_key, mf_short, mf_long, mf_low, mf_high)
        if mult != 1.0:
            eff_min_value = args.min_value * 1e8 * mult
            print(f"  [market_flow] {diag} → 하한 {args.min_value:.0f}억 × {mult:.3f} "
                  f"= {eff_min_value/1e8:,.0f}억")
        else:
            print(f"  [market_flow] {diag} → 하한 {args.min_value:.0f}억 유지")
    _to_base  = float(getattr(args, "turnover_gate_base", 1.0))
    _to_slope = float(getattr(args, "turnover_gate_slope", 15.0))
    _to_cap   = float(getattr(args, "turnover_cap_pct", 200.0))
    _min_now = _to_base + _to_slope * frac
    print(f"  [회전율 게이트] base {_to_base:g}%% + slope {_to_slope:g}%% × frac {frac:.2f} "
          f"= 현시각 최저 {_min_now:.2f}%%")
    if getattr(args, "theme", False):
        print("  [테마 모드] 네이버 테마 기반 선별")
        res = find_leaders_by_theme(rank_df, args.vol_mult, frac,
                                    min_value=eff_min_value,
                                    min_mktcap=args.min_mktcap * 1e8,
                                    max_change=args.max_change,
                                    theme_min_change=args.theme_min_change,
                                    rise_min=args.rise_min,
                                    hot_min=args.hot_min,
                                    turnover_gate_base=_to_base,
                                    turnover_gate_slope=_to_slope,
                                    turnover_cap_pct=_to_cap)
    else:
        res = find_leaders(rank_df, args.rise_min, args.hot_min, args.vol_mult, frac,
                           min_value=eff_min_value,
                           min_mktcap=args.min_mktcap * 1e8,
                           max_change=args.max_change,
                           turnover_gate_base=_to_base,
                           turnover_gate_slope=_to_slope,
                           turnover_cap_pct=_to_cap)
    if getattr(args, "summary_only", False):
        # 웹 버튼용: 디스코드 형식 요약만 stdout 출력 (표 생략, 디스코드는 전송)
        print(_summary_text(res, args, frac, start_dt))
    else:
        _report(rank_df, res, args, frac, start_dt)
    # 재선별(--reval)은 전환 판정용 내부 스냅샷 → 디스코드 스팸 방지 위해 알림 생략.
    if not getattr(args, "reval", False):
        _discord_notify(res, args, frac, start_dt)
    _save_picks(res, args, frac, start_dt)
    _save_avgval_cache()
    _save_trend_cache()


def _wait_until(hh: int, mm: int) -> None:
    """오늘 hh:mm 까지 대기. 이미 지났으면 즉시 반환."""
    target = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    while True:
        now = datetime.now()
        if now >= target:
            return
        remain = (target - now).total_seconds()
        print(f"[{now:%H:%M:%S}] {hh:02d}:{mm:02d} 선별까지 {remain/60:.0f}분 대기…", flush=True)
        time.sleep(min(remain, 30))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=str, default="10:00",
                    help="선별 시각 HH:MM (이 시각까지 9시부터의 누적 거래대금으로 1회 선별)")
    ap.add_argument("--top", type=int, default=100, help="거래대금 상위 N")
    ap.add_argument("--rise-min", type=float, default=3.0, help="상승 종목 등락률 하한 %")
    ap.add_argument("--hot-min", type=int, default=3, help="핫섹터 최소 상승종목 수 (기본 3)")
    ap.add_argument("--vol-mult", type=float, default=2.0, help="거래대금 평소대비 배수 게이트")
    ap.add_argument("--min-value", type=float, default=500.0, help="거래대금 최소 절대값 (억원, 기본 500)")
    ap.add_argument("--dyn-value-pct", type=float, default=0.0,
                    help="§2 동적 거래대금(legacy): 유니버스 거래대금 합의 N%%를 종목별 하한으로 사용. "
                         "0(기본)=미적용, market_flow 배수 사용. >0이면 market_flow 무시하고 legacy 우선.")
    ap.add_argument("--mf-window-short", type=int, default=5,
                    help="market_flow: 최근 N일 평균(분자). 기본 5")
    ap.add_argument("--mf-window-long", type=int, default=20,
                    help="market_flow: 기준 M일 평균(분모). 기본 20")
    ap.add_argument("--mf-clamp-low", type=float, default=0.5,
                    help="market_flow: 배수 하한(침체기 방어). 기본 0.5")
    ap.add_argument("--mf-clamp-high", type=float, default=2.0,
                    help="market_flow: 배수 상한(활황기 방어). 기본 2.0")
    ap.add_argument("--min-mktcap", type=float, default=1000.0, help="시가총액 최소 (억원, 기본 1000)")
    ap.add_argument("--max-change", type=float, default=25.0,
                    help="등락률 상한 %% — 과열주 제외 (기본 25). 상한가30%%-익절4%%-여유1%%: "
                         "진입 후 +4%% 익절 여력 없는 과열주는 대장주 후보에서 제외")
    # ── Level1: 회전율(유통주식수 근사) 시간대 계단 게이트 + 극단치 캡 ──
    ap.add_argument("--turnover-gate-base", type=float, default=1.0,
                    help="시간대 계단 게이트 base(%%). 요구회전율 = base + slope × 세션경과율. "
                         "항상 활성. 예: 1.0(기본) → 09:30 최저 1%%, 15:20 최저 16%%.")
    ap.add_argument("--turnover-gate-slope", type=float, default=15.0,
                    help="시간대 계단 게이트 기울기(%%). base+slope 가 15:20 최대치. (기본 15)")
    ap.add_argument("--turnover-cap-pct", type=float, default=200.0,
                    help="회전율 pctile 입력값 극단치 캡(%%). 0=무제한. (기본 200)")
    ap.add_argument("--once", action="store_true", help="대기 없이 지금 즉시 1회(테스트)")
    ap.add_argument("--include-etf", action="store_true", help="ETF/ETN 포함(기본 제외)")
    ap.add_argument("--ignore-hours", action="store_true", help="장시간 무시하고 실행")
    ap.add_argument("--theme", action="store_true", help="테마 기반 선별 모드 (기본: 업종 기반)")
    ap.add_argument("--summary-only", action="store_true",
                    help="웹 버튼용: stdout에는 디스코드 형식 요약만 출력(표 생략). 디스코드 전송은 유지")
    ap.add_argument("--suppress-empty-alert", action="store_true",
                    help="미선별(대장주 없음) 시 디스코드 알림 생략. 러너가 마지막 시도(13:00 직전) "
                         "전 재시도에 붙여 '없음' 스팸을 막는다. 선별 성공 알림은 항상 전송")
    ap.add_argument("--reval", action="store_true",
                    help="섹터 전환용 장중 재선별: 선별 로직은 동일하되 결과를 "
                         "<날짜>_reval.json 으로 저장(정본 <날짜>.json 보존)하고 디스코드 전송 생략. "
                         "leader_runner 가 전환 판정용으로 주기 호출")
    ap.add_argument("--theme-min-change", type=float, default=-100.0,
                    help="테마 모드: 핫테마 최소 '테마 등락률' %% (기본 -100=비활성). "
                         "테마 전체가 하락이어도 내부 급등주를 잡기 위해 기본은 종목 상승률로만 판정")
    args = ap.parse_args()

    _load_avgval_cache()
    _load_trend_cache()
    if not getattr(args, "summary_only", False):
        print(f"대장주 탐색기 | 코스피+코스닥 각{args.top}(통합상위{args.top*2}) 상승+{args.rise_min:g}% "
              f"핫섹터{args.hot_min}+ 거래대금{args.vol_mult:g}배 | "
              f"{'즉시1회' if args.once else f'{args.at} 선별'}")

    if args.once:
        if not args.ignore_hours and not _is_trading_day():
            print("  휴장일(공휴일/대체공휴일 포함) — 선별 생략(테스트는 --ignore-hours)")
            return
        run_once(args)
        return

    # 지정 시각까지 대기 후 1회 선별 (9시부터의 누적 거래대금이 그 시점에 반영됨)
    try:
        hh, mm = (int(x) for x in args.at.split(":"))
    except Exception:
        print(f"  [오류] --at 형식은 HH:MM 이어야 함 (입력: {args.at})")
        return
    if not args.ignore_hours and not _is_trading_day():
        print("  휴장일(공휴일/대체공휴일 포함) — 선별 생략(테스트는 --once --ignore-hours)")
        return
    _wait_until(hh, mm)
    run_once(args)


if __name__ == "__main__":
    main()
