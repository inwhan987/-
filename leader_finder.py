"""대장주(섹터 리더) 탐색기 — 장중 거래대금 상위에서 주도 섹터/종목 추출.

알고리즘 (사용자 설계):
  1) 장 시작 후, 거래대금 상위 100 (코스피+코스닥 통합)
  2) 그중 많이 상승하는 종목 추림 (등락률 >= RISE_MIN_PCT)
  3) 추려진 상승 종목들의 섹터(네이버 업종) 집계 → 주도(핫) 섹터 식별
  4) 각 핫섹터 안에서 상승률 1위 종목 선정,
     단 거래대금이 평소(최근 5거래일 평균) 대비 VOL_MULT 배 이상일 것
     (장중이므로 세션 경과 비율로 평균을 보정해 비교)

데이터 소스:
  - 거래대금 순위/등락률/현재가 : KIS 통합(KRX+NXT) 거래대금 (kis_quant, 1순위)
      · 네이버 sise_quant 는 KRX/NXT 분리로 고거래대금 종목 누락 → 폴백으로만 유지
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

# 거래대금 순위 소스:
#   1순위 KIS 통합(KRX+NXT) 거래대금 — 네이버 sise_quant 는 KRX/NXT 분리로 고가·고거래대금
#   종목(레인보우로보틱스 등)이 유니버스에서 누락돼 hot 섹터 판정이 왜곡됨(2026-08-03 교체).
#   naver_quant 는 폴백(KIS 완전 실패 시) + 보통주필터/re-export 호환용으로 유지.
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
    """리스트를 0~1 백분위수로 변환(동순위=같은 값). n=1이면 [0.5]."""
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


def _fetch_investor_flow(codes: list[str]) -> tuple[dict, bool, str]:
    """KIS 기관+외국인 수급 조회 — 3-tier fallback.

    Tier 1: KIS 당일 실시간(FHKST01010100 UN) → 모든 종목 성공 시 사용.
    Tier 2: 당일 조회 실패 종목 있을 때 → KIS 5거래일 히스토리(FHPTJ04160001 UN)
            연속 순매수일수(0~5)를 대체값으로. 히스토리도 실패하면 0 처리.
    Tier 3: Tier 1 전체 예외 → 수급 가중치 제거 fallback (flow_ok=False).

    반환: (flow_dict, flow_ok, tier_label)
    - flow_ok=True  : Tier 1 또는 Tier 2 성공 → 정상 가중치 사용
    - flow_ok=False : Tier 3(전체 실패) → 수급 가중치 제거 fallback
    - tier_label    : 디스코드·로그 표시용 "T1" / "T2" / "T3"
    """
    import os
    from datetime import date as _date

    def _discord(msg: str) -> None:
        url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not url:
            return
        try:
            import requests as _req
            _req.post(url, json={"content": msg, "username": "대장주알림"}, timeout=8)
        except Exception:
            pass

    try:
        from kis_quant import fetch_investor_netbuy, fetch_investor_history_5d
        from stock_bot.broker import KISBroker
        broker = KISBroker()
        try:
            raw = fetch_investor_netbuy(broker, codes)
            failed_codes = [c for c, v in raw.items() if v is None]
            ok_cnt = len(codes) - len(failed_codes)

            if not failed_codes:
                # Tier 1 완전 성공
                flow_dict = {k: float(v) for k, v in raw.items()}
                tier = "T1"
                msg = (f"👑 수급 Tier1(당일실시간) {ok_cnt}/{len(codes)}건 성공")
                print(f"  [수급 {msg}]")
                _discord(msg)
                return flow_dict, True, tier

            # Tier 2: 실패 종목만 히스토리 5일로 보완
            today_str = _date.today().strftime("%Y%m%d")
            hist = fetch_investor_history_5d(broker, failed_codes, today_str)
            flow_dict: dict = {}
            for code, val in raw.items():
                if val is not None:
                    flow_dict[code] = float(val)
                else:
                    h = hist.get(code)
                    # 연속순매수일수(0~5)를 수량 대용으로. None(히스토리도 실패)=0
                    flow_dict[code] = float(h) if h is not None else 0.0
            hist_ok = sum(1 for c in failed_codes if hist.get(c) is not None)
            tier = "T2"
            msg = (f"👑 수급 Tier2(T1 {ok_cnt}성공/{len(failed_codes)}실패→히스토리보완 "
                   f"{hist_ok}/{len(failed_codes)}건)")
            print(f"  [수급 {msg}]")
            _discord(msg)
            return flow_dict, True, tier

        finally:
            broker.close()

    except Exception as e:
        tier = "T3"
        msg = f"👑 수급 Tier3(전체 실패→가중치 제거) {e}"
        print(f"  [수급 {msg}]")
        _discord(msg)
        return {}, False, tier


def _stock_weights() -> tuple[float, float, float, float, float]:
    """종목 점수 가중치(log거래대금, 수급, 상승률, 회전율, 급증배율).

    settings 에서 읽어 파라미터탭 핫리로드를 반영한다. 실패 시 기본값 사용.
    """
    try:
        from stock_bot.config.settings import settings as _s
        return (
            float(getattr(_s, "lead_st_w_value",    0.35)),
            float(getattr(_s, "lead_st_w_flow",     0.20)),
            float(getattr(_s, "lead_st_w_updn",     0.15)),
            float(getattr(_s, "lead_st_w_turnover", 0.15)),
            float(getattr(_s, "lead_st_w_surge",    0.15)),
        )
    except Exception:
        return (0.35, 0.20, 0.15, 0.15, 0.15)


def _stock_weights_nf() -> tuple[float, float, float, float, float]:
    """수급 조회 실패 시 fallback 가중치 — 수급 항목 제거(0), 나머지 재분배.

    기본: log거래대금 40% + 상승률 20% + 회전율 20% + 급증배율 20%.
    """
    try:
        from stock_bot.config.settings import settings as _s
        return (
            float(getattr(_s, "lead_st_nf_w_value",    0.40)),
            0.0,
            float(getattr(_s, "lead_st_nf_w_updn",     0.20)),
            float(getattr(_s, "lead_st_nf_w_turnover", 0.20)),
            float(getattr(_s, "lead_st_nf_w_surge",    0.20)),
        )
    except Exception:
        return (0.40, 0.0, 0.20, 0.20, 0.20)


def _sector_weights() -> tuple[float, float]:
    """섹터 점수 가중치(강도 intensity, 균등도 breadth).

    sector_score = mean(stock_scores) × (w_int + w_br × breadth)
    breadth = mean / max — 1종목 집중 시 ≈0, 고르게 상승 시 ≈1.
    """
    try:
        from stock_bot.config.settings import settings as _s
        return (
            float(getattr(_s, "lead_sc_w_intensity", 0.65)),
            float(getattr(_s, "lead_sc_w_breadth",   0.35)),
        )
    except Exception:
        return (0.65, 0.35)


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
                          hot_min: int = 3) -> dict:
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

    # ── Step 0: 자격 종목 사전 산정 — 4개 조건 모두 통과 (업종 모드와 동일) ──
    #   등락 rise_min%↑ + 거래대금 min_value↑ + 시총 min_mktcap↑ + 평소대비 vol_mult배↑
    #   핫섹터 강도(riser_count)와 대장주 후보 모두 '자격 종목'만으로 판정한다.
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
        qual_rows.append({**row.to_dict(), "vol_ratio": ratio})
    if not qual_rows:
        return {"hot_sectors": [], "leaders": []}
    qual_df = pd.DataFrame(qual_rows).reset_index(drop=True)

    # ── 수급 주입 및 종목 점수 계산 ──────────────────────────────────
    inv_flow, flow_ok, _flow_tier = _fetch_investor_flow(qual_df["code"].tolist())
    qual_df["investor_netbuy"] = qual_df["code"].map(inv_flow).fillna(0.0)
    n_st = len(qual_df)
    if n_st > 0:
        _vals   = qual_df["value_won"].tolist()
        _netbuy = qual_df["investor_netbuy"].tolist()
        _chg    = qual_df["change_pct"].tolist()
        _vr     = qual_df["vol_ratio"].tolist()
        _mktcap = qual_df.get("market_cap", pd.Series([0.0] * n_st)).tolist()
        _to     = [v / m if m > 0 else 0.0 for v, m in zip(_vals, _mktcap)]
        pc_lv  = _pctile([math.log(max(v, 1)) for v in _vals])
        pc_nb  = _pctile(_netbuy)
        pc_chg = _pctile(_chg)
        pc_to  = _pctile(_to)
        pc_vr  = _pctile(_vr)
        _w = _stock_weights() if flow_ok else _stock_weights_nf()
        stock_scores: dict[str, float] = {
            qual_df.at[i, "code"]: (
                pc_lv[i] * _w[0] + pc_nb[i] * _w[1] +
                pc_chg[i] * _w[2] + pc_to[i] * _w[3] + pc_vr[i] * _w[4]
            )
            for i in range(n_st)
        }
    else:
        stock_scores = {}

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
    # 상한가 포함 자격 전체 종목(qual_codes)의 stock_scores 사용.
    w_int, w_br = _sector_weights()
    for a in accepted:
        sc_vals = [stock_scores[c] for c in a["qual_codes"] if c in stock_scores]
        if not sc_vals:
            a["sector_score"] = 0.0
            continue
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
            "top3": top3,
        })
        seen_codes.add(row["code"])

    # 대장주 순위: sector_score 기준(점수 시스템으로 통일)
    leaders.sort(key=lambda x: x.get("sector_score", 0), reverse=True)
    hot_list.sort(key=lambda x: x.get("sector_score", 0), reverse=True)
    return {"hot_sectors": hot_list, "leaders": leaders}


def find_leaders(rank_df: pd.DataFrame, rise_min: float, hot_min: int,
                 vol_mult: float, frac: float,
                 min_value: float = 500e8,
                 min_mktcap: float = 1000e8,
                 max_change: float = 29.5) -> dict:
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
    qualified_rows = []
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
        qualified_rows.append({**row.to_dict(), "vol_ratio": ratio})

    if not qualified_rows:
        return {"hot_sectors": [], "leaders": []}

    qual_df = pd.DataFrame(qualified_rows).reset_index(drop=True)

    # ── 수급(기관+외국인 순매수) 주입 ────────────────────────────────
    inv_flow, flow_ok, _flow_tier = _fetch_investor_flow(qual_df["code"].tolist())
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
        _mktcap  = qual_df.get("market_cap", pd.Series([0.0] * n_st)).tolist()
        _to      = [v / m if m > 0 else 0.0 for v, m in zip(_vals, _mktcap)]
        pc_lv  = _pctile([math.log(max(v, 1)) for v in _vals])
        pc_nb  = _pctile(_netbuy)
        pc_chg = _pctile(_chg)
        pc_to  = _pctile(_to)
        pc_vr  = _pctile(_vr)
        _w = _stock_weights() if flow_ok else _stock_weights_nf()
        stock_scores: dict[str, float] = {
            qual_df.at[i, "code"]: (
                pc_lv[i] * _w[0] + pc_nb[i] * _w[1] +
                pc_chg[i] * _w[2] + pc_to[i] * _w[3] + pc_vr[i] * _w[4]
            )
            for i in range(n_st)
        }
    else:
        stock_scores = {}

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
        return {"hot_sectors": [], "leaders": []}

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
            "top3": top3,
        })
    return {"hot_sectors": hot, "leaders": leaders}


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
    lines = [f"**📊 대장주 선별 [{now}] [{mode_tag}]** | 세션경과 {frac*100:.0f}%"]

    if leaders:
        lines.append("")
        lines.append("**🏆 대장주 후보**")
        for i, L in enumerate(leaders, 1):
            lines.append(
                f"`{i}위` **{L['name']}** ({L['code']})  "
                f"{L['change_pct']:+.1f}%  "
                f"거래대금 {L['value_won']/1e8:.0f}억  "
                f"평소대비 {L['vol_ratio']:.1f}x"
            )
            lines.append(
                f"　　　섹터: {L['sector']} (상승 {L.get('sector_risers', 0)}종목)"
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
        "leaders": [
            {"code": L["code"], "name": L["name"], "sector": L["sector"],
             "change_pct": round(float(L["change_pct"]), 2),
             "price": float(L["price"]),
             "value_won": float(L["value_won"]),
             "vol_ratio": round(float(L["vol_ratio"]), 2),
             # 섹터 강도(상승종목 수) — 섹터 전환 히스테리시스 판정용. 정렬 키와 동일.
             "sector_risers": int(L.get("sector_risers", 0) or 0),
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
    # KIS 통합 거래대금(1순위) — 실패(빈 결과) 시에만 네이버로 폴백해 선별 blackout 방지.
    try:
        rank_df = kis_quant.fetch_ranking(
            top_n=args.top, stock_only=not args.include_etf,
            min_value=args.min_value * 1e8)
    except Exception as e:
        print(f"  [KIS 거래대금 실패 → 네이버 폴백] {e}")
        rank_df = pd.DataFrame()
    if rank_df is None or rank_df.empty:
        # 1차 폴백: 네이버 KRX+NXT 통합(nxt_sise_quant 합산) — KIS 통합값에 근접.
        print("  [폴백] KIS 빈 결과 → 네이버 KRX+NXT 통합 거래대금 사용")
        try:
            rank_df = fetch_ranking_unified(top_n=args.top, stock_only=not args.include_etf)
        except Exception as e:
            print(f"  [네이버 통합 실패 → KRX 단독 폴백] {e}")
            rank_df = pd.DataFrame()
        # 2차 폴백: 통합도 빈 결과면 KRX 단독(sise_quant)이라도 사용해 blackout 방지.
        if rank_df is None or rank_df.empty:
            print("  [폴백2] 네이버 통합 빈 결과 → 네이버 KRX 단독(sise_quant)")
            rank_df = fetch_ranking(top_n=args.top, stock_only=not args.include_etf)
    if rank_df.empty:
        print("  [경고] 순위 데이터 수집 실패")
        return
    if getattr(args, "theme", False):
        print("  [테마 모드] 네이버 테마 기반 선별")
        res = find_leaders_by_theme(rank_df, args.vol_mult, frac,
                                    min_value=args.min_value * 1e8,
                                    min_mktcap=args.min_mktcap * 1e8,
                                    max_change=args.max_change,
                                    theme_min_change=args.theme_min_change,
                                    rise_min=args.rise_min,
                                    hot_min=args.hot_min)
    else:
        res = find_leaders(rank_df, args.rise_min, args.hot_min, args.vol_mult, frac,
                           min_value=args.min_value * 1e8,
                           min_mktcap=args.min_mktcap * 1e8,
                           max_change=args.max_change)
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
    ap.add_argument("--min-mktcap", type=float, default=1000.0, help="시가총액 최소 (억원, 기본 1000)")
    ap.add_argument("--max-change", type=float, default=25.0,
                    help="등락률 상한 %% — 과열주 제외 (기본 25). 상한가30%%-익절4%%-여유1%%: "
                         "진입 후 +4%% 익절 여력 없는 과열주는 대장주 후보에서 제외")
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
