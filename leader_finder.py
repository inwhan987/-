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
_THEME_CACHE_PATH = _CACHE_DIR / "leader_theme_cache.json"
_SECTOR_CACHE_PATH = _CACHE_DIR / "leader_sector_cache.json"

# 거래대금 순위 소스 (2026-08-09 재교체):
#   1순위 네이버 KRX+NXT 통합(fetch_ranking_unified) — 시장×거래소별 페이지 4건 크롤링
#   후 종목코드 기준으로 KRX·NXT 거래대금 합산. KIS 통합값에 근접하면서 유량 무제한.
#   3분 주기 reval 도입으로 KIS UN 재조회 α건 부담이 stock-bot 체결 지연을 유발할
#   위험이 있어 스왑. 실시간성은 네이버 스크래핑 시차(수초) 감수.
#   2순위 KIS 통합(kis_quant.fetch_ranking) — 네이버 실패 시 폴백. 시장×거래소 4건
#   유니버스 소스(2026-08-10 정책, KRX 단독 + ETF 제외):
#     1순위 매경(mk_quant.fetch_ranking) — SSR·인증불요·KRX 통합 거래대금
#     2순위 KIS KRX(kis_quant.fetch_ranking) — 매경 실패 시 폴백. NXT/UN 미사용.
#   시가총액은 매경 시총 랭킹을 장 시작 전 1회 캐시(mk_quant.fetch_marketcap_map).
import kis_quant
import mk_quant  # 시가총액 캐시(매경) — 실시간 필요없어 유지
import daum_quant  # 거래대금 순위(다음) — 실시간, 매경 지연 문제 대체(2026-08-11)
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
# code -> {"date": "YYYYMMDD", "avg": float, "w": 창일수}
# "w" 는 2026-08-19 창 5→20 변경분. 창이 다른 캐시는 miss 로 취급해 재계산한다
# (창이 다른 평균값을 현행 창의 평균인 척 쓰면 배수가 조용히 틀어진다).
_AVGVAL_CACHE: dict[str, dict] = {}
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


# ── market_flow: 시장유동성 배수 캐시 (KRX-only, 2026-08-10) ────────────
# 매 선별 시점에 상위N 거래대금 합(base_sum, 원단위)을 날짜별로 기록.
# 스키마 v2 (KRX-only): mk_quant/kis_quant KRX-only 값과 pykrx KRX-only 백필
# 값이 같은 스케일. prefetch 시 UN 스케일 업 없음(2026-08-10 매경 전환).
# 파일 구조: {"__schema__": "krx_permkt_v2", "20260810": {"kospi": 12345.0, "kosdaq": 6789.0}, ...}
# permkt_v2 (2026-08-11): 시장별 top_n 합을 **분리** 저장 → 배수 계산도 시장별.
#   이유: 코스피/코스닥 활황 사이클이 달라(테마장 = 코스닥 폭발) 통합 합계로는
#   코스닥 활황이 코스피 하한까지 밀어올려 코스피 종목 자격 통과가 어려워짐.
_MF_SCHEMA = "krx_permkt_v2"

# 시장 유동성 배수의 비교 창 = 20영업일 (2026-08-19, 기존 5영업일).
# 5일창은 직전 주 자체가 활황/침체면 그 편향이 그대로 분모가 되어 배수가 1.0
# 근처로 눌리고(활황 뒤 활황), 연휴·이벤트 하루가 평균을 크게 흔들었다.
# 20일창(약 한 달)은 그날의 시장 유량을 '평상시 한 달' 대비로 보게 한다.
# 캐시 보존은 60~80일이라 20일창을 채우고도 여유가 있다.
# 주의: 종목별 '평소대비 배수'(avg_value_nd)의 5일창과는 별개 개념 — 그쪽은 유지.
MF_WINDOW_D = 20


def _load_market_flow() -> dict[str, dict[str, float]]:
    """market_flow 캐시 로드. 스키마 mismatch 시 자동 wipe.

    v2: 값이 {"kospi": float, "kosdaq": float} dict. 구 v1(단일 float)은 폐기.
    """
    try:
        if _MARKET_FLOW_PATH.exists():
            data = json.loads(_MARKET_FLOW_PATH.read_text(encoding="utf-8"))
            if data.get("__schema__") != _MF_SCHEMA:
                return {}
            out: dict[str, dict[str, float]] = {}
            for k, v in data.items():
                if k == "__schema__" or not isinstance(v, dict):
                    continue
                kospi = float(v.get("kospi", 0.0) or 0.0)
                kosdaq = float(v.get("kosdaq", 0.0) or 0.0)
                if kospi > 0 or kosdaq > 0:
                    out[str(k)] = {"kospi": kospi, "kosdaq": kosdaq}
            return out
    except Exception:
        pass
    return {}


def _save_market_flow(cache: dict[str, dict[str, float]]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out: dict = {"__schema__": _MF_SCHEMA}
        for k, v in cache.items():
            if isinstance(v, dict) and (v.get("kospi", 0) > 0 or v.get("kosdaq", 0) > 0):
                out[k] = {"kospi": float(v.get("kospi", 0.0)),
                          "kosdaq": float(v.get("kosdaq", 0.0))}
        _MARKET_FLOW_PATH.write_text(
            json.dumps(out, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
    except Exception:
        pass


def _record_market_flow(today_key: str, kospi_sum: float, kosdaq_sum: float) -> None:
    """오늘 시장별 top-N 합을 캐시에 기록. 각 시장 최댓값 유지 = 마감 근사."""
    if (not kospi_sum or kospi_sum <= 0) and (not kosdaq_sum or kosdaq_sum <= 0):
        return
    cache = _load_market_flow()
    cur = cache.get(today_key, {"kospi": 0.0, "kosdaq": 0.0})
    new_k = max(float(cur.get("kospi", 0.0)), float(kospi_sum or 0.0))
    new_q = max(float(cur.get("kosdaq", 0.0)), float(kosdaq_sum or 0.0))
    cache[today_key] = {"kospi": new_k, "kosdaq": new_q}
    if len(cache) > 80:
        keys_sorted = sorted(cache.keys(), reverse=True)
        cache = {k: cache[k] for k in keys_sorted[:60]}
    _save_market_flow(cache)


def prefetch_market_flow(days: int = MF_WINDOW_D, top_n: int = 200) -> tuple[int, int, str]:
    """pykrx KRX-only 최근 N영업일 top-N 거래대금 합을 저장(2026-08-10 KRX 단독).

    · pykrx get_market_ohlcv_by_ticker(market="ALL") 는 이미 KRX 정규시장 값이고
      라이브(mk_quant/kis_quant) 도 KRX-only 라 스케일 일치 — 별도 보정 없음.
    · 이미 있는 키는 건너뛴다(idempotent). 라이브 값이 있으면 그것을 우선.
    · 반환: (신규추가일수, 캐시총일수, 진단문자열).
    """
    try:
        from pykrx import stock as krx
        from datetime import date, timedelta as _td
    except Exception as e:
        return 0, 0, f"pykrx import 실패: {e}"

    cache = _load_market_flow()
    added = 0
    today = date.today()

    # "오늘 제외 최근 N영업일 창" 을 먼저 열거한 뒤 그 안에서만 미충족 키를 채운다.
    # 오늘은 라이브(rank_df) 로 계산되고, 캐시의 오늘 키는 내일 이후에서 쓰이므로
    # 백필 대상은 어제 이전 N일로 한정. (2026-08-11)
    # 평일 기준으로만 세면 공휴일이 슬롯을 잡아먹어 실제 거래일이 days 에 못 미친다
    # (창 20일이면 설·추석 낀 달에 16~17일밖에 안 남음). 그래서 고정 개수가 아니라
    # "유효값 days 개를 확보할 때까지" 뒤로 스캔한다. 상한(_MF_MAX_SCAN)은 무한
    # 후퇴 방지용 — 20일창이면 최대 50평일(약 10주)까지만 거슬러 본다. (2026-08-19)
    _MF_MAX_SCAN = days * 2 + 10
    targets = []
    d = today - _td(days=1)
    while len(targets) < _MF_MAX_SCAN:
        if d.weekday() < 5:
            targets.append(d)
        d -= _td(days=1)

    # 과거일자는 pykrx close 값이 진실 — 낮 라이브가 남긴 부분값을 반드시 덮어쓴다.
    # 라이브가 max로 갱신하는 값은 13:00 부근 부분합(선별창 종료 시점)이라
    # 실제 15:30 마감 대비 과소. 스킵하면 캐시에 부정확 값이 영구히 남음. (2026-08-11)
    # 창이 20일로 늘면서(2026-08-19) 매 08:30·부팅마다 20일 × 2콜 = 40 pykrx 콜은
    # 과하다. 라이브 부분값이 남아 있을 수 있는 구간은 최근 며칠뿐이고, 한 번
    # pykrx close 로 덮인 과거일은 그 뒤로 바뀌지 않는다.
    # → 최근 _MF_FORCE_DAYS 일만 강제 덮어쓰기, 그보다 오래된 날은 값이 없을 때만 채운다.
    _MF_FORCE_DAYS = 5
    overwritten = 0        # 값이 실제로 바뀐 날 수
    diff_lines: list[str] = []  # 큰 변화(±5% 이상) 상세
    skipped = 0
    filled = 0             # 유효값 확보 일수 — days 에 닿으면 스캔 종료(휴장일 보정)
    for idx, dd in enumerate(targets):
        if filled >= days:
            break
        key = dd.strftime("%Y%m%d")
        if idx >= _MF_FORCE_DAYS:
            _cur = cache.get(key)
            if (isinstance(_cur, dict) and float(_cur.get("kospi", 0.0)) > 0
                    and float(_cur.get("kosdaq", 0.0)) > 0):
                skipped += 1
                filled += 1
                continue
        # 시장별 top_n 합 — 라이브 daum_quant(시장당 top_n) 와 스케일 일치.
        try:
            k_df = krx.get_market_ohlcv_by_ticker(key, market="KOSPI")
            q_df = krx.get_market_ohlcv_by_ticker(key, market="KOSDAQ")
        except Exception:
            continue
        def _sum_top(df):
            if df is not None and not df.empty and "거래대금" in df.columns:
                return float(df["거래대금"].astype(float).nlargest(top_n).sum())
            return 0.0
        kospi_sum = _sum_top(k_df)
        kosdaq_sum = _sum_top(q_df)
        if kospi_sum > 0 or kosdaq_sum > 0:
            filled += 1    # 휴장일(빈 df)은 세지 않는다 → 그만큼 더 뒤로 스캔
            prev = cache.get(key)
            prev_k = float(prev.get("kospi", 0.0)) if isinstance(prev, dict) else 0.0
            prev_q = float(prev.get("kosdaq", 0.0)) if isinstance(prev, dict) else 0.0
            cache[key] = {"kospi": kospi_sum, "kosdaq": kosdaq_sum}  # 강제 덮어쓰기
            if prev_k <= 0 and prev_q <= 0:
                added += 1
            elif abs(kospi_sum - prev_k) > 1.0 or abs(kosdaq_sum - prev_q) > 1.0:
                # 값이 실제로 갱신됨 → 이전값 vs 새값 진단 출력
                overwritten += 1
                diff_lines.append(
                    f"{key} 덮어씀: 코스피 {prev_k/1e12:.2f}→{kospi_sum/1e12:.2f}조 · "
                    f"코스닥 {prev_q/1e12:.2f}→{kosdaq_sum/1e12:.2f}조"
                )

    # 오늘 seed 제거(2026-08-11) — 08:30 매경은 장전 미결값이라 오염원.
    # 오늘값은 run_once 매 사이클 _record_market_flow 가 라이브로 갱신하고,
    # 오늘 배수 계산은 캐시가 아닌 today_val 파라미터를 직접 씀. 캐시의 오늘
    # 키는 '내일 이후 분모' 용도이며 그건 내일 08:30 pykrx 백필로 커버됨.

    _save_market_flow(cache)
    # 캐시 상세: 날짜별 kospi/kosdaq 합계(조원). 최신 → 오래된 순.
    day_lines = []
    for k in sorted(cache.keys(), reverse=True):
        if k.startswith("__"):
            continue
        v = cache.get(k)
        if not isinstance(v, dict):
            continue
        ks = float(v.get("kospi", 0)) / 1e12
        kq = float(v.get("kosdaq", 0)) / 1e12
        day_lines.append(f"{k}: 코스피 {ks:.2f}조 · 코스닥 {kq:.2f}조")
    detail = "\n  · ".join(day_lines) if day_lines else "(빈캐시)"
    diff_block = ("\n  · " + "\n  · ".join(diff_lines)) if diff_lines else ""
    return added, len(cache), (
        f"신규 {added}일 · 덮어씀 {overwritten}일 · 기존유지 {skipped}일 · "
        f"확보 {filled}/{days}일 / "
        f"캐시 총 {len(cache)}일 · KRX-only (요청 {days}일 · top {top_n})" + diff_block +
        "\n  · " + detail
    )


# ── 시각비례 유량 배수 ───────────────────────────────────────────
# market_flow(하루완결 close 값) × frac(t) 선형근사 단일 경로.
# 15:35 마감 스냅샷 1회로 close 값 캐시 → 다음날 이후 매 시각 frac 비례로 baseline 계산.
_INTRADAY_SLOT_MIN = 10  # 10분 슬롯 (진단 태그용)


def _slot_key(now: datetime) -> str:
    m = (now.minute // _INTRADAY_SLOT_MIN) * _INTRADAY_SLOT_MIN
    return f"{now.hour:02d}:{m:02d}"


def _compute_intraday_flow_multiplier(today_key: str, slot: str,
                                       today_kospi: float, today_kosdaq: float,
                                       frac: float, low: float, high: float,
                                       need_days: int = 3,
                                       window: int = MF_WINDOW_D,
                                       ) -> tuple[dict[str, float], str]:
    """시장별 오늘 top-N 합 / 시장별 과거 close 평균 × frac(t).

    반환: ({"kospi": mult, "kosdaq": mult}, 진단문자열).
    각 시장 표본 부족 시 해당 시장만 1.0. 클램프 [low, high].
    """
    mf = _load_market_flow()
    prior = sorted([k for k in mf.keys() if k < today_key], reverse=True)[:window]

    def _one(name: str, today_val: float) -> tuple[float, str]:
        if today_val <= 0:
            return 1.0, f"{name} today 0 → 1.0"
        samples = [mf[k].get(name, 0.0) for k in prior if mf[k].get(name, 0.0) > 0]
        if len(samples) < need_days:
            return 1.0, f"{name} 표본 {len(samples)}일 부족 → 1.0"
        avg_full = sum(samples) / len(samples)
        f = max(float(frac), 0.02)
        baseline = avg_full * f
        raw = today_val / baseline if baseline > 0 else 1.0
        mult = max(float(low), min(float(high), raw))
        return mult, (f"{name} 오늘 {today_val/1e8:,.0f}억 / "
                      f"완결{len(samples)}일avg{avg_full/1e8:,.0f}억×frac{f:.2f}"
                      f"={baseline/1e8:,.0f}억 = raw{raw:.3f}→{mult:.3f}")

    m_k, d_k = _one("kospi", today_kospi)
    m_q, d_q = _one("kosdaq", today_kosdaq)
    diag = f"[유량 {slot}] KOSPI {d_k} | KOSDAQ {d_q}"
    return {"kospi": m_k, "kosdaq": m_q}, diag


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


# ── 2) 평균 거래대금 (KIS KRX 일봉, 일 1회 캐시) ─────────────────────
# 종목 '평소대비 배수'의 분모 창 = 5거래일 (유지). 20일창은 시장 유동성 배수
# (market_flow) 쪽에만 적용한다 — 종목 배수는 "요 며칠 대비 오늘 얼마나
# 터졌나"를 봐야 해서 짧은 창이 맞고, 시장 유량은 활황/침체 사이클을 걸러야
# 해서 긴 창이 맞다. (2026-08-19)
# ── 선별 단계별 소요 계측 (2026-08-19) ────────────────────────────────
# 09:30 선별이 70초 걸린 원인을 로그만으로 좁히지 못했다. 프로세스 기동
# 12.5초를 빼도 본체가 58초인데, 로컬에서 잰 네트워크 단계(다음 랭킹 0.96초 ·
# 네이버 테마목록 1.67초 · 테마 구성종목 0.82초)는 다 합쳐야 3.5초고 avgval 은
# 캐시 히트라 0초다. 즉 파이에서만 느려지는 구간이 따로 있다는 뜻이라,
# 추측 대신 매 회차가 자기 소요를 스스로 보고하게 한다.
_STAGE_T0 = None
_STAGES: list = []


def _stage_reset() -> None:
    global _STAGE_T0, _STAGES
    _STAGE_T0 = time.time()
    _STAGES = []


def _stage(label: str) -> None:
    """직전 마크 이후 경과를 label 로 적립. _stage_reset() 전이면 무시."""
    global _STAGE_T0
    if _STAGE_T0 is None:
        return
    now = time.time()
    _STAGES.append((label, now - _STAGE_T0))
    _STAGE_T0 = now


def _stage_report() -> str:
    if not _STAGES:
        return ""
    tot = sum(d for _, d in _STAGES)
    body = " · ".join(f"{lbl} {d:.1f}s" for lbl, d in _STAGES)
    return f"  [단계별 소요] 합계 {tot:.1f}s = {body}"


AVGVAL_WINDOW_D = 5

_LEADER_BROKER = None  # KIS UN 일봉용 lazy singleton


def _get_leader_broker():
    """avg_value_nd 내부용 broker 지연 초기화. 최초 1회만 생성."""
    global _LEADER_BROKER
    if _LEADER_BROKER is None:
        try:
            from stock_bot.broker import KISBroker
            _LEADER_BROKER = KISBroker()
        except Exception:
            _LEADER_BROKER = False  # 재시도 방지 sentinel
    return _LEADER_BROKER if _LEADER_BROKER else None


def avg_value_nd(code: str, window: int = AVGVAL_WINDOW_D) -> float:
    """최근 window(기본 AVGVAL_WINDOW_D=5)거래일 평균 일중 거래대금(원, KRX 단독 — 2026-08-10 정책).

    1순위 KIS J(KRX) 일봉(kis_quant.avg_value_nd_krx).
    실패 시 pykrx 폴백(동일 KRX 스케일). 유니버스(매경·KIS)가 KRX 기준이라
    평소대비 배수 분모도 KRX 로 통일.
    조회 실패 시 최대 3회 리트라이하고, 그래도 실패하면 직전 거래일 캐시값으로
    폴백한다(평소 거래량은 하루로 거의 변하지 않음). 캐시도 없으면 0.0.
    창(window)이 다른 캐시 엔트리는 히트로 인정하지 않는다 — 5일창 시절 값이
    다른 창의 평균인 척 살아남으면 배수가 조용히 틀어지기 때문(2026-08-19).
    """
    today = datetime.now().strftime("%Y%m%d")
    c = _AVGVAL_CACHE.get(code)
    if (c and c.get("date") == today and c.get("avg", 0) > 0
            and int(c.get("w", 5)) == int(window)):
        return float(c["avg"])
    avg = 0.0
    # ── 1순위: KIS KRX 일봉 ──
    broker = _get_leader_broker()
    if broker is not None:
        try:
            from kis_quant import avg_value_nd_krx
            avg = avg_value_nd_krx(broker, code, today, window=window)
        except Exception:
            avg = 0.0
    # ── 2순위: pykrx (KRX only, 폴백) ──
    if avg <= 0:
        for attempt in range(3):
            try:
                from pykrx import stock as krx
                end = datetime.now()
                # window 거래일 확보용 여유 구간(휴일·주말 감안, KIS 쪽과 동일 식)
                start = end - timedelta(days=max(21, int(window * 2.2) + 10))
                df = krx.get_market_ohlcv_by_date(
                    start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
                )
                if df is not None and not df.empty and {"거래량", "종가"} <= set(df.columns):
                    val = (df["거래량"].astype(float) * df["종가"].astype(float))
                    val = val[val > 0]
                    if len(val) >= 2:
                        # 당일(마지막 행, 미완성) 제외 → 직전 최대 window 거래일 평균
                        hist = val.iloc[:-1]
                        avg = float(hist.tail(window).mean()) if len(hist) >= 1 else 0.0
            except Exception:
                avg = 0.0
            if avg > 0:
                break
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    if avg > 0:
        _AVGVAL_CACHE[code] = {"date": today, "avg": avg, "w": int(window)}
        return avg
    # ── 최종 폴백: 오늘 조회 실패 → 직전 거래일 캐시값 재사용 ──
    # 창이 맞는 과거값만 재사용한다(창 변경 직후엔 폴백 없이 0.0 → "히스토리없음").
    if c and c.get("avg", 0) > 0 and int(c.get("w", 5)) == int(window):
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


def _load_sector_cache() -> None:
    """업종(sector_of) 디스크 캐시 로드 (2026-08-19).

    러너는 선별 tick 마다 leader_finder 를 새 서브프로세스로 띄우므로 모듈 전역
    _SECTOR_CACHE / _GROUP_CACHE 가 매 회차 비어 있었다. 그래서 유니버스 200종목
    전부에 대해 네이버 종목→업종번호→업종명 2단 크롤을 매번 재실행(실측 70초).
    종목의 업종은 상장 기간 내내 사실상 불변이라 날짜 키 없이 영구 캐시한다.

    주의: 조회 실패시 sector_of 가 넣는 "(미상)" 는 저장하지 않는다. 날짜 만료가
    없는 캐시라 일시적 네트워크 실패값을 굳히면 영구 오염되기 때문.
    """
    global _SECTOR_CACHE, _GROUP_CACHE
    try:
        if not _SECTOR_CACHE_PATH.exists():
            return
        raw = json.loads(_SECTOR_CACHE_PATH.read_text(encoding="utf-8"))
        _SECTOR_CACHE = {k: v for k, v in (raw.get("codes") or {}).items()
                         if v and v != "(미상)"}
        _GROUP_CACHE = {k: v for k, v in (raw.get("groups") or {}).items() if v}
    except Exception:
        _SECTOR_CACHE, _GROUP_CACHE = {}, {}


def _save_sector_cache() -> None:
    try:
        if not _SECTOR_CACHE:
            return
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _SECTOR_CACHE_PATH.write_text(json.dumps({
            "v": 1,
            # "(미상)"/빈값은 제외 — 다음 회차에 재시도되게 남겨둔다.
            "codes": {k: v for k, v in _SECTOR_CACHE.items() if v and v != "(미상)"},
            "groups": {k: v for k, v in _GROUP_CACHE.items() if v},
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def prefetch_sectors(codes: list[str], pace_sec: float = 0.0) -> None:
    """업종 프리페치 — 02시 avgval 크론이 같은 유니버스로 이어서 호출.

    codes 는 prefetch_avgval 이 쓰는 것과 동일한 '시총≥min_cap' 집합.
    네이버 크롤이라 KIS 유량과 무관하지만 예의상 pace_sec 를 열어둔다.
    """
    t0 = time.time()
    _load_sector_cache()
    todo = [c for c in codes if c not in _SECTOR_CACHE]
    hit = len(codes) - len(todo)
    print(f"[prefetch_sectors] 대상 {len(codes)}종목 · 기존캐시 hit {hit} · 조회 {len(todo)}")
    if not todo:
        print("[prefetch_sectors] 전부 캐시 hit — 종료")
        return
    ok = fail = 0
    for i, code in enumerate(todo, 1):
        sec = sector_of(code)
        if sec and sec != "(미상)":
            ok += 1
        else:
            # 실패값은 캐시에 굳히지 않는다 — 다음 회차 재시도용으로 지운다.
            _SECTOR_CACHE.pop(code, None)
            fail += 1
        if i % 100 == 0:
            print(f"[prefetch_sectors] {i}/{len(todo)} 진행 (성공 {ok}·실패 {fail}) "
                  f"경과 {time.time() - t0:.0f}초")
            _save_sector_cache()
        if pace_sec > 0 and i < len(todo):
            time.sleep(pace_sec)
    _save_sector_cache()
    print(f"[prefetch_sectors] 완료: 유니버스 {len(codes)} · 기존캐시 {hit} 제외 · "
          f"조회 {len(todo)} · 성공 {ok} · 실패 {fail} · "
          f"업종그룹 {len(_GROUP_CACHE)} · 소요 {time.time() - t0:.0f}초")


def sector_of(code: str, *, allow_fetch: bool = True) -> str:
    """종목 업종명. 캐시 미스면 네이버 상세페이지를 크롤한다(1건당 0.5~2초).

    allow_fetch=False 는 '캐시에 있으면 쓰고, 없으면 포기' 모드다. 진단 표시처럼
    값이 없어도 무방한 곳에서 쓴다. 미스를 캐시에 굳히지 않으므로 같은 코드를
    나중에 진짜로 필요해서 부르면 그때 정상 조회된다.

    2026-08-19: 자격탈락 종목 진단(diag["near"])이 유니버스 전수(200종목)에
    이 함수를 부르고 있었다. 실측 미스는 7건(약 2초)이라 70초 선별의 주범은
    아니었지만, 캐시가 비거나 신규 상장이 몰린 날엔 그대로 수십 초가 되는
    구조라 막아둔다. 바로 위 4번 관문(avg_value_nd)은 같은 이유로 이미
    게이트가 걸려 있었는데(2026-08-12) 여기만 빠져 있었다.
    """
    if code in _SECTOR_CACHE:
        return _SECTOR_CACHE[code]
    if not allow_fetch:
        return "(미상)"
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
_THEME_CACHE_DATE = ""                    # 디스크 캐시에 적재된 날짜(YYYYMMDD)


def _load_theme_cache() -> None:
    """테마 구성종목 디스크 캐시 로드 (2026-08-19).

    러너는 선별 tick 마다 leader_finder 를 **새 서브프로세스**로 띄우므로
    모듈 전역 _THEME_STOCK_CACHE 가 매 회차 비어 있었다. 그래서 263개 테마
    상세 페이지를 매번 재크롤링(약 80~120초) → 09:28:30 시작해도 종료가
    09:31 을 넘고, 미선별 재시도 회차마다 같은 크롤을 반복했다.
    테마 구성종목은 하루 중 사실상 불변이라 날짜 키로 디스크에 캐시한다.
    (등락률이 들어있는 '테마 목록'은 실시간 값이므로 캐시하지 않는다.)
    """
    global _THEME_STOCK_CACHE, _THEME_CACHE_DATE
    try:
        if not _THEME_CACHE_PATH.exists():
            return
        raw = json.loads(_THEME_CACHE_PATH.read_text(encoding="utf-8"))
        if raw.get("date") != datetime.now().strftime("%Y%m%d"):
            return  # 어제 캐시 → 버림
        _THEME_STOCK_CACHE = {k: set(v) for k, v in (raw.get("themes") or {}).items()}
        _THEME_CACHE_DATE = raw.get("date", "")
    except Exception:
        _THEME_STOCK_CACHE = {}


def _save_theme_cache() -> None:
    try:
        if not _THEME_STOCK_CACHE:
            return
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _THEME_CACHE_PATH.write_text(json.dumps({
            "date": datetime.now().strftime("%Y%m%d"),
            "themes": {k: sorted(v) for k, v in _THEME_STOCK_CACHE.items()},
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def prefetch_themes() -> None:
    """09:05 크론용 테마 구성종목 프리페치.

    선별 전에 263개 테마를 미리 긁어 디스크 캐시에 넣어둔다. 09:30 첫 tick 과
    이후 재시도 tick 이 전부 캐시 히트 → 선별 소요가 ~90초에서 10초 안쪽으로.
    장 시작(09:00) 직후에 도는 이유: 테마 편입/제외가 개장 무렵 반영될 수
    있어 장전 캐시는 그날 구성과 어긋날 수 있다.
    """
    t0 = time.time()
    _load_theme_cache()
    hit0 = len(_THEME_STOCK_CACHE)
    themes = fetch_theme_list(min_change=-100.0)
    if not themes:
        print("[prefetch_themes] 테마 목록 0 — 종료")
        return
    ok = fail = 0
    for th in themes:
        if any(x in th["name"].lower() for x in ("밸류업", "value-up", "value up")):
            continue
        if fetch_theme_stocks(th["no"]):
            ok += 1
        else:
            fail += 1
    _save_theme_cache()
    print(f"[prefetch_themes] 완료: 테마 {len(themes)} · 성공 {ok} · 실패 {fail} · "
          f"기존히트 {hit0} · 소요 {time.time() - t0:.0f}초")


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
    """log10(거래대금원) 절대 정규화. 100억=0, 5000억=1(대형주 제외 max≈8천억)."""
    if v <= 0:
        return 0.0
    lv = math.log10(v)
    return max(0.0, min(1.0, (lv - 10.0) / 1.699))


def _abs_netbuy(v: float) -> float:
    """수급(주) 절대 정규화. 0=0.5 중립, +20만주=1.0, -20만주=0.0."""
    return max(0.0, min(1.0, 0.5 + v / 400000.0))


def _abs_change(v: float) -> float:
    """등락률 절대 정규화. 5%=0, 30%=1 (상한제 근처)."""
    return max(0.0, min(1.0, (v - 5.0) / 25.0))


def _abs_turnover(v: float) -> float:
    """회전율 절대 정규화. 0%=0, 30%=1."""
    return max(0.0, min(1.0, v / 30.0))


def _abs_vol_ratio(v: float) -> float:
    """평소대비 배수 절대 정규화(로그). 1배=0, 30배=1. 로그로 저배율 구분력↑."""
    if v <= 1:
        return 0.0
    return max(0.0, min(1.0, math.log10(v) / math.log10(30)))


def _compute_score_parts(vals, netbuy, chg, to, vr):
    """5개 절대앵커 리스트 반환. pctile(상대순위) 사용 안 함 — 3종목 강제
    [1, 0.5, 0] 매핑으로 절대값이 근접해도 3등이 억울하게 탈락하는 문제 회피."""
    n = len(vals)
    if n == 0:
        return [], [], [], [], []
    return ([_abs_log_value(v) for v in vals],
            [_abs_netbuy(v) for v in netbuy],
            [_abs_change(v) for v in chg],
            [_abs_turnover(v) for v in to],
            [_abs_vol_ratio(v) for v in vr])


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
    # 2026-08-11: 수급 조회 완전 제거.
    # KIS 개별종목 실시간 순매수는 공식 API 미제공 (inquire-price 는 개별종목
    # frgn_ntby_qty=0 · orgn_ntby_qty 필드 없음, inquire-investor 는 EOD 기준
    # 당일 빈값). D-1 값으로 대장주 판정하는 건 부적절 → NF 가중치만 사용.
    # KIS 호출 유량도 자격통과 종목수만큼 절약(직렬 모의 1/초).
    print(f"  [수급 OFF] KIS 개별종목 실시간 수급 미제공 → 수급 가중치 제거 가중치로 선별 ({len(codes)}종목)")
    return {}, False, "OFF"


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
    """종목 점수 가중치(log거래대금, 수급=0, 상승률, 회전율, 급증배율).

    2026-08-11: 수급 완전 제거. 예전 `_stock_weights` (수급 포함) 와
    `_stock_weights_nf` (수급 실패시 폴백) 를 병합. 이제 폴백 값이 default.
    수급 슬롯은 리턴 튜플 shape 유지 위해 0.0 으로 고정.
    합≠100% 입력도 _normalize 가 자동 정규화한다(§3).
    """
    try:
        from stock_bot.config.settings import settings as _s
        w = (
            float(getattr(_s, "lead_st_w_value",    0.40)),
            0.0,  # 수급 슬롯 - deprecated, 항상 0
            float(getattr(_s, "lead_st_w_updn",     0.40)),
            float(getattr(_s, "lead_st_w_turnover", 0.11)),
            float(getattr(_s, "lead_st_w_surge",    0.09)),
        )
    except Exception:
        w = (0.40, 0.0, 0.40, 0.11, 0.09)
    return _normalize(w)  # type: ignore[return-value]


# 표시용 100점 변환은 leader_trader 로그·디스코드도 같이 써야 해서 공용 모듈에 있다.
from stock_bot.lead_score import _to_display, to_display_stock, to_display_sector  # noqa: E402,F401


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


# ── 세션 경과 비율 ──────────────────────────────────────────────────
def _session_fraction(now: datetime | None = None) -> float:
    now = now or datetime.now()
    start = now.replace(hour=_SESSION_START[0], minute=_SESSION_START[1], second=0, microsecond=0)
    elapsed = (now - start).total_seconds() / 60.0
    frac = elapsed / _SESSION_MIN
    return min(max(frac, 0.02), 1.0)  # 너무 이른 시각엔 하한 2%


# pick 창(09:00~switch_until) 진행률. 거래대금 하한 시간비례에만 사용.
# session_fraction(390분·15:30)과 분리 — pick 은 13시에 종료되므로 게이트가
# pick 창 내에서 스케일되어야 오후에 과도한 하한이 안 걸림.
# 실제 거래대금은 U자형(초반 30~40% 집중)이라 선형 근사는 근사치일 뿐.
def _pick_fraction(now: datetime | None = None, until_hhmm: str = "13:00") -> float:
    now = now or datetime.now()
    start = now.replace(hour=_SESSION_START[0], minute=_SESSION_START[1], second=0, microsecond=0)
    try:
        hh, mm = until_hhmm.split(":")
        end_min = int(hh) * 60 + int(mm) - (_SESSION_START[0] * 60 + _SESSION_START[1])
    except (ValueError, AttributeError):
        end_min = 240  # fallback: 09:00 ~ 13:00
    end_min = max(30, end_min)  # 최소 30분 방어
    elapsed = (now - start).total_seconds() / 60.0
    frac = elapsed / float(end_min)
    return min(max(frac, 0.02), 1.0)


# ── 4) 대장주 선별 ──────────────────────────────────────────────────
def find_leaders_by_theme(rank_df: pd.DataFrame, vol_mult: float, frac: float,
                          min_value: float = 500e8, min_mktcap: float = 1000e8,
                          max_change: float = 29.5,
                          theme_min_change: float = -100.0,
                          rise_min: float = 3.0,
                          hot_min: int = 3,
                          turnover_cap_pct: float = 200.0,
                          min_value_by_market: dict[str, float] | None = None) -> dict:
    """테마 기반 대장주 선별.

    ① 거래대금 상위 rank_df (기존)
    ② 네이버 핫테마 목록 (등락률 theme_min_change% 이상)
    ③ 핫테마 ∩ rank_df 교집합에서 상승종목 hot_min개 이상인 테마만
    ④ 후보 중 거래대금·상승률 조건 통과한 상승률 1위 = 대장주
    """
    # 진단 dict — 선별 실패 시 왜 실패했는지 후속 소비자(로그·디스코드)에
    # 노출한다. 모든 return 경로에 diag 를 붙여야 함.
    diag: dict = {
        "universe": int(len(rank_df)),
        # 순차 필터(funnel) 순서로 정렬: 시총 → 거래대금 → 등락률 → 평소대비 (4단, 회전율 게이트 폐지)
        # 각 카운터 = 앞 관문을 모두 통과한 후 이 관문에서 탈락한 종목 수
        "drops": {"mktcap": 0, "value": 0, "rise": 0, "vol_mult": 0},
        "rise_min": float(rise_min), "min_value": float(min_value),
        "min_value_by_market": {k: float(v) for k, v in (min_value_by_market or {}).items()},
        "min_mktcap": float(min_mktcap), "vol_mult": float(vol_mult),
        "max_change": float(max_change),
        "near": [],
        "per_gate": {"mktcap": [], "value": [], "rise": [], "vol_mult": []},
        "hot_min": int(hot_min),
        "sector_counts": {},
        "qualified": [],
        "reason": "",
        "value_source": "",  # run_once 가 채움: market_flow/dyn_legacy/fixed
    }
    if rank_df.empty:
        diag["reason"] = "유니버스 0(rank_df empty)"
        return {"hot_sectors": [], "leaders": [], "diag": diag}

    # 핫테마 가져오기
    hot_themes = fetch_theme_list(min_change=theme_min_change)
    _stage("테마목록")
    if not hot_themes:
        diag["reason"] = "핫테마 목록 0(테마 미형성)"
        return {"hot_sectors": [], "leaders": [], "diag": diag}

    # ── Step 0: 자격 종목 사전 산정 — 5개 조건 모두 통과 ──
    #   등락 rise_min%↑ + 거래대금 min_value↑ + 시총 min_mktcap↑ + 평소대비 vol_mult배↑
    #   + (Level1 신규) 시간대 계단 회전율 하한(base>0 활성).
    #   핫섹터 강도(riser_count)와 대장주 후보 모두 '자격 종목'만으로 판정한다.
    qual_rows = []
    for _, row in rank_df.iterrows():
        fails: list[str] = []
        passes = 0
        # funnel 순서로 검사: 시총 → 거래대금 → 등락률 → 평소대비 → 회전율
        # 1) 시총 (market_cap==0이면 무조건 pass)
        mcap = float(row.get("market_cap", 0) or 0)
        if mcap > 0 and mcap < min_mktcap:
            fails.append(f"시총 {mcap/1e8:,.0f}<{min_mktcap/1e8:,.0f}억")
        else:
            passes += 1
        # 2) 거래대금 — 시장별 하한 우선(min_value_by_market), 없으면 min_value 스칼라.
        _mkt = str(row.get("market", "")).upper()
        _thr = float(min_value_by_market.get(_mkt, min_value)) if min_value_by_market else float(min_value)
        if row["value_won"] < _thr:
            fails.append(f"거래대금 {float(row['value_won'])/1e8:,.0f}<{_thr/1e8:,.0f}억")
        else:
            passes += 1
        # 3) 등락률
        if row["change_pct"] < rise_min:
            fails.append(f"등락 {float(row['change_pct']):+.2f}%<{rise_min:g}%")
        else:
            passes += 1
        # 4) 평소대비 배수 — 1~3단 통과 종목만 계산(네트워크 호출이라 탈락 확정
        #    종목까지 조회하면 캐시미스 시 선별이 수 분씩 늘어짐, 2026-08-12).
        if passes == 3:
            avg_n = avg_value_nd(row["code"])
            expected = avg_n * frac if avg_n > 0 else 0.0
            ratio = row["value_won"] / expected if expected > 0 else 0.0
            if avg_n <= 0:
                fails.append("평소대비(히스토리없음)")
            elif ratio < vol_mult:
                fails.append(f"평소대비 {ratio:.2f}<{vol_mult:g}배")
            else:
                passes += 1
        else:
            ratio = 0.0
        # 5) 회전율 — 게이트 폐지(2026-08-11). 값은 계산해서 섹터·종목강도 점수에만 사용.
        to_pct = _turnover_pct_row(row)

        if passes == 4:
            qual_rows.append({**row.to_dict(), "vol_ratio": ratio, "turnover_pct": to_pct})
            continue

        # drops 카운터는 순차 funnel: 시총→거래대금→등락→평소대비 순으로
        # 첫 번째 실패한 관문에 1건 귀속(앞 관문 통과분만 다음 관문에서 셈해짐)
        for _f in fails:
            key = None
            if _f.startswith("시총"):
                key = "mktcap"
            elif _f.startswith("거래대금"):
                key = "value"
            elif _f.startswith("등락"):
                key = "rise"
            elif _f.startswith("평소대비"):
                key = "vol_mult"
            if key:
                diag["drops"][key] += 1
                break  # funnel: 첫 관문에서만 카운트

        diag["near"].append({
            "code": str(row.get("code", "")),
            "name": str(row.get("name", "")),
            # 진단 표시 전용 — 캐시 히트만 사용(allow_fetch=False).
            # 선별 결과에는 안 쓰이므로 이걸 위해 네이버를 긁을 이유가 없다.
            "sector": sector_of(row.get("code", ""), allow_fetch=False),
            "change_pct": float(row.get("change_pct", 0) or 0),
            "value_eok": float(row.get("value_won", 0) or 0) / 1e8,
            "passes": passes,
            "fails": fails,
            "fail": fails[0] if fails else "",
        })
    # 2~3개 통과(=2~3개 탈락) 종목만 최대 10개, 통과수 desc → 등락률 desc
    diag["near"] = sorted(
        [n for n in diag["near"] if n.get("passes", 0) in (2, 3)],
        key=lambda x: (x.get("passes", 0), x.get("change_pct", 0)),
        reverse=True,
    )[:10]
    if not qual_rows:
        diag["reason"] = "자격 통과 종목 0"
        return {"hot_sectors": [], "leaders": [], "diag": diag}
    qual_df = pd.DataFrame(qual_rows).reset_index(drop=True)

    # ── 수급 주입 및 종목 점수 계산 ──────────────────────────────────
    inv_flow, flow_ok, flow_tier = _fetch_investor_flow(qual_df["code"].tolist())
    qual_df["investor_netbuy"] = qual_df["code"].map(inv_flow).fillna(0.0)
    n_st = len(qual_df)
    _stage("자격판정")
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
        _w = _stock_weights()  # 수급 제거(2026-08-11) — flow_ok 상관없이 항상 동일
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

    # 진단: 자격 종목의 섹터 분포 + 종목점수 리스트
    try:
        if "sector" not in qual_df.columns:
            qual_df["sector"] = qual_df["code"].map(sector_of)
        _stage("업종라벨")
        for _sec_k, _sec_g in qual_df.groupby("sector"):
            diag["sector_counts"][str(_sec_k)] = int(len(_sec_g))
        for i in range(len(qual_df)):
            _code = qual_df.at[i, "code"]
            diag["qualified"].append({
                "code": str(_code),
                "name": str(qual_df.at[i, "name"]),
                "sector": str(qual_df.at[i, "sector"]) if "sector" in qual_df.columns
                          else sector_of(str(_code)),
                "change_pct": float(qual_df.at[i, "change_pct"]),
                "value_eok": float(qual_df.at[i, "value_won"]) / 1e8,
                "vol_ratio": float(qual_df.at[i, "vol_ratio"]),
                "turnover_pct": float(qual_df.at[i, "turnover_pct"]),
                "stock_score": float(stock_scores.get(_code, 0.0)),
                "parts": stock_score_parts.get(_code, {}),
                "mktcap_eok": float(qual_df.at[i, "market_cap"]) / 1e8
                              if "market_cap" in qual_df.columns else 0.0,
            })
        diag["qualified"].sort(key=lambda x: x["stock_score"], reverse=True)
    except Exception:
        pass

    # ── Step 1: 핫테마 후보 수집 (자격 종목 hot_min개↑ 테마만) ──────────
    OVERLAP_THR = 0.5   # 교집합/작은쪽 >= 50% 이면 같은 섹터로 간주
    # 지수성·광범위 테마 제외: 정부 밸류업 정책 묶음은 대형 상승주를 거의 다
    # 포함해 진짜 섹터(반도체 장비 등)를 가리는 노이즈이므로 후보에서 뺀다.
    THEME_EXCLUDE = ("밸류업", "value-up", "value up")
    theme_pool: list[dict] = []   # {"theme", "cands", "cand_codes", "riser_count"}

    code_theme: dict[str, str] = {}   # code → theme name (진단용 라벨)
    for theme in hot_themes:
        name_l = theme["name"].lower()
        if any(x in name_l for x in THEME_EXCLUDE):
            continue
        t_codes = fetch_theme_stocks(theme["no"])
        # 진단 라벨용: 이 테마 소속 모든 종목에 테마명 기입(첫 매칭 우선).
        for _c in t_codes:
            code_theme.setdefault(_c, theme["name"])
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
        # 섹터 강도: 어차피 매매는 Top3만 하므로 Top3 기준 집계로 통일.
        _top3 = _mem.head(3)
        total_val = float(_top3["value_won"].sum())
        total_mktcap = float(_top3["market_cap"].sum()) if "market_cap" in _top3.columns else 0.0
        avg_vr = float(_top3["vol_ratio"].mean())
        # 커플링: 대장(1위) vs 후발(2~3위) 동반 계수
        _sorted_chg = _mem["change_pct"].tolist()
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
            "avg_change": float(_top3["change_pct"].mean()),
            "members": members,
        })

    # 진단 라벨 재기입: 업종(sector_of) → 네이버 테마명 (매칭 안되면 업종 유지).
    # 사용자 관찰(2026-08-11): 대장주 선정은 테마 기반인데 로그의 [xxx] 는 업종으로
    # 노출돼 혼선. 테마 매칭된 종목은 테마명으로, 아니면 원래 업종 라벨 유지.
    for _n in diag.get("near", []):
        _tn = code_theme.get(_n.get("code", ""))
        if _tn:
            _n["sector"] = _tn
    for _q in diag.get("qualified", []):
        _tn = code_theme.get(_q.get("code", ""))
        if _tn:
            _q["sector"] = _tn
    # sector_counts 도 테마 기준 재집계 (매칭 안된 종목은 원래 업종으로 카운트).
    _sc: dict[str, int] = {}
    for _q in diag.get("qualified", []):
        _sc[str(_q.get("sector", ""))] = _sc.get(str(_q.get("sector", "")), 0) + 1
    if _sc:
        diag["sector_counts"] = _sc

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

    _stage("테마집계")
    if not accepted:
        _hn = sum(1 for _ in theme_pool)
        diag["reason"] = (f"자격 {len(qual_df)}종목 통과, 후보 테마 {_hn}개 → "
                          f"중복 병합 후 hot_min({hot_min})↑ 잔존 테마 0")
        return {"hot_sectors": [], "leaders": [], "diag": diag}

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
            "sector_score_100": to_display_sector(sector_score),
            "members": item.get("members", []),
        })

        # 종목 점수 기준으로 정렬 후 top3 바스켓 구성
        cands["_sc"] = cands["code"].map(stock_scores).fillna(0.0)
        cands = cands.sort_values("_sc", ascending=False)
        avail = [r for r in cands.to_dict("records") if r["code"] not in seen_codes]
        if not avail:
            continue
        # 일봉추세 게이트(기본 비활성) — 켜지면 추세 down(정배열 붕괴+MA60 하회)인
        # 1등 후보를 건너뛰고 그 다음 순위 후보를 대장주로 채택. top3 바스켓 구성은
        # 게이트 적용 전 순위(avail) 기준 그대로 유지.
        from stock_bot.config.settings import settings as _s
        if bool(getattr(_s, "leader_daily_trend_gate", False)):
            _picked = None
            for _cand in avail:
                _dt = daily_trend_of(_cand["code"])
                if _dt and _dt.get("trend") == "down":
                    continue
                _picked = _cand
                break
            if _picked is None:
                continue
            avail = [_picked] + [r for r in avail if r["code"] != _picked["code"]]
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
                "stock_score_100": to_display_stock(float(r["_sc"])),
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
            "sector_score_100": to_display_sector(sector_score),
            "theme_change": item["avg_change"],
            "stock_score": round(lead_score, 4),
            "stock_score_100": to_display_stock(lead_score),
            "score_parts": stock_score_parts.get(row["code"], {}),
            "netbuy": float(row.get("investor_netbuy", 0) or 0),
            "top3": top3,
        })
        _p = stock_score_parts.get(row["code"], {})
        if _p:
            _mode_kr = "상대순위" if _p.get("mode") == "pctile" else "절대점수(n=1)"
            _w_now = _stock_weights()  # 수급 제거(2026-08-11) — flow_ok 상관없이 항상 동일
            print(f"  [{row['code']} {row['name']}] 종합점수 {to_display_stock(lead_score):.1f}점 "
                  f"(raw {lead_score:.3f})  ({_mode_kr})")
            print(f"     거래대금 {_p.get('lv',0):.2f}×{_w_now[0]*100:.0f}%  |  "
                  f"수급 {_p.get('nb',0):.2f}×{_w_now[1]*100:.0f}%  |  "
                  f"등락률 {_p.get('chg',0):.2f}×{_w_now[2]*100:.0f}%  |  "
                  f"회전율 {_p.get('to',0):.2f}×{_w_now[3]*100:.0f}%  |  "
                  f"급증배수 {_p.get('vr',0):.2f}×{_w_now[4]*100:.0f}%")
        seen_codes.add(row["code"])

    # 대장주 순위: sector_score 기준(점수 시스템으로 통일)
    leaders.sort(key=lambda x: x.get("sector_score", 0), reverse=True)
    hot_list.sort(key=lambda x: x.get("sector_score", 0), reverse=True)
    if not leaders:
        diag["reason"] = (f"핫섹터 {len(hot_list)}개 있지만 대장주 후보 없음"
                          f"(과열컷 {max_change:g}%초과 배제 등)")
    # 수급 조회 상태 — 표시측(선별 알림·대시보드)에서 '수급O/수급없음' 배지에 사용.
    return {"hot_sectors": hot_list, "leaders": leaders,
            "flow_ok": bool(flow_ok), "flow_tier": flow_tier,
            "diag": diag}


def criteria_text(diag: dict) -> str:
    """현재 선별 조건 + funnel 요약(성공·실패 공통).

    예전엔 실패했을 때만 이 블록이 찍혀서, 선별에 성공하면 "무슨 기준으로
    걸러진 결과인지"가 로그·디스코드 어디에도 안 남았다. 조건은 파라미터
    변경·거래대금 동적배수로 매일 바뀌므로 성공 시에도 같이 남긴다.
    """
    dr = diag.get("drops") or {}
    qc = sum((diag.get("sector_counts") or {}).values())
    by_mkt = diag.get("min_value_by_market") or {}
    if by_mkt:
        val_s = "거래대금≥(" + " · ".join(
            f"{k} {float(v)/1e8:,.0f}억" for k, v in by_mkt.items()) + ")"
    else:
        val_s = f"거래대금≥{diag.get('min_value', 0)/1e8:,.0f}억"
    cond = (f"기준: 등락≥{diag.get('rise_min', 0):g}% · {val_s} · "
            f"시총≥{diag.get('min_mktcap', 0)/1e8:,.0f}억 · "
            f"평소×{diag.get('vol_mult', 0):g}")
    if diag.get("max_change"):
        cond += f" · 과열컷 {float(diag['max_change']):g}%↑ 제외"
    cond += f" · 핫섹터 상승 {diag.get('hot_min', 0)}종목↑"
    out = [f"유니버스 {diag.get('universe', 0)} → 자격통과 {qc}", cond]
    if diag.get("value_source"):
        out.append(f"거래대금 소스: {diag['value_source']}")
    out.append("funnel 탈락(첫 관문에서만 카운트): "
               f"시총{dr.get('mktcap', 0)} → 거래대금{dr.get('value', 0)} → "
               f"등락{dr.get('rise', 0)} → 평소대비{dr.get('vol_mult', 0)}")
    return "\n".join(out)


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
        print(f"\n■ 주도(핫) 섹터  (상승 {args.hot_min}개+ · sector_score순 · Top3 기준 집계)")
        print(f"{'섹터':<20} {'상승수':>6} {'Top3거래대금(억)':>18} {'Top3평균등락':>12}")
        print("-" * 60)
        for s in hot[:8]:
            print(f"{s['sector']:<20} {s['riser_count']:>6} "
                  f"{s['total_value']/1e8:>17,.0f} {s['avg_change']:>+11.2f}%")
    else:
        print("\n  핫섹터 없음 (상승 종목이 섹터별로 충분치 않음)")

    # 수급 제거(2026-08-11) — KIS 실시간 미제공. 배지 문구는 유지하되 중립화.
    _flow_ok = bool(res.get("flow_ok", False))
    print(f"\n■ 수급: OFF (KIS 개별종목 실시간 미제공 · 수급 가중치 제거 가중치로 선별)")

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
            print(f"{'':<18}   └ 점수: 섹터 {L.get('sector_score_100', 0):.1f}점"
                  f"(raw {float(L.get('sector_score',0) or 0):.3f})"
                  f" · 종목 {L.get('stock_score_100', 0):.1f}점"
                  f"(raw {float(L.get('stock_score',0) or 0):.3f}){_nb_s}")
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
        # 성공 시에도 어떤 조건으로 걸러진 결과인지 남긴다(조건은 매일 바뀜).
        _dg = res.get("diag") or {}
        if _dg:
            print("\n■ 선별 조건 · funnel")
            for _cl in criteria_text(_dg).split("\n"):
                print(f"    · {_cl}")
    else:
        print("  조건 충족 대장주 없음")
        _dg = res.get("diag") or {}
        if _dg:
            for _cl in criteria_text(_dg).split("\n"):
                print(f"    · {_cl}")
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
                          f"점수 {to_display_stock(_q['stock_score']):.1f}점({_mode}) · "
                          f"{_q['change_pct']:+6.2f}% · {_q['value_eok']:>6,.0f}억 · "
                          f"회전 {_q['turnover_pct']:>5.2f}% · 평소{_q['vol_ratio']:>4.2f}x · "
                          f"[{_q['sector']}]")
            _nr = _dg.get("near") or []
            if _nr:
                print(f"    · 아깝게 탈락 (2~3개 통과, 최대 10개):")
                for _n in _nr[:10]:
                    _fails = _n.get("fails") or ([_n.get("fail")] if _n.get("fail") else [])
                    _reason = ", ".join(_fails)
                    print(f"        [{_n['code']} {_n['name'][:12]:<12}] "
                          f"통과 {_n.get('passes', 0)}/4 · "
                          f"{_n['change_pct']:+6.2f}% · {_n['value_eok']:>6,.0f}억 · "
                          f"[{_n['sector']}] ← {_reason}")
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
        ("SYMBOLS", "TRADE_SYMBOLS", "LEADER_BAND_RATIO", "LEADER_OWN_SYMBOL_PRIORITY"))
    try:
        ratio = float(kv.get("LEADER_BAND_RATIO", "0.6") or 0.6)
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

    mode_tag = "🗂️테마"
    # 수급 표시 제거(2026-08-18): 2026-08-11에 수급 가중치가 0이 돼 선별에
    # 전혀 안 쓰이는데 알림에는 계속 나와 오해 소지만 있었다. 콘솔·대시보드·
    # picks JSON 의 netbuy 는 관측용으로 그대로 둔다.
    lines = [f"**📊 대장주 선별 [{now}] [{mode_tag}]** | 세션경과 {frac*100:.0f}%"]

    if leaders:
        # 선별 조건 — 성공했을 때도 "어떤 기준으로 나온 결과인지" 같이 보여준다.
        # 거래대금 하한은 시장상황 동적배수로 매일 달라져서, 결과만 보면 판단 불가.
        _dg = res.get("diag") or {}
        if _dg:
            lines.append("")
            lines.append("```" + "\n" + criteria_text(_dg) + "\n" + "```")
        lines.append("")
        lines.append("**🏆 대장주 후보** (섹터점수 순)")
        for i, L in enumerate(leaders, 1):
            lines.append(
                f"`{i}위` **{L['name']}** ({L['code']})  "
                f"{L['change_pct']:+.1f}%  "
                f"거래대금 {L['value_won']/1e8:.0f}억  "
                f"평소대비 {L['vol_ratio']:.1f}x"
            )
            lines.append(
                f"　　　섹터: {L['sector']} · 섹터점수 {to_display_sector(float(L.get('sector_score', 0) or 0)):.1f}점 "
                f"· 종목점수 {to_display_stock(float(L.get('stock_score', 0) or 0)):.1f}점 "
                f"(상승 {L.get('sector_risers', 0)}종목)"
            )

        # 매매 바스켓 — 실제 매매봇(leader_trader)이 감시하는 섹터 전부(최대
        # leader_max_sectors개) 를 섹터 60%룰로 추려, 섹터별 top3에 종목 60%룰을
        # 그대로 적용해 미리보기. 예전엔 leaders[0](1등 섹터)만 보여줘서 2·3등
        # 섹터가 추가돼도 알림에서 안 보였음(대시보드 basket 도 동일 버그, 별도 수정).
        # 왜 일부 후보가 빠지는지(예: 스톡봇 보유종목·비율 미달·섹터밴드 미달) 확인용.
        # own-symbol 우선권(점유락)이 켜져 있으면 스톡봇과 겹쳐도 제외하지 않고,
        # 먼저 잡는 봇이 가져간다 → leader_trader.py 판정과 동일하게 표시.
        ratio, own, own_priority = _basket_rule_params()
        try:
            from stock_bot.config.settings import settings as _s
            max_sectors = max(1, int(getattr(_s, "leader_max_sectors", 3) or 3))
        except Exception:
            max_sectors = 3
        top_sector_score = float(leaders[0].get("sector_score", 0) or 0)
        band_leaders = [leaders[0]] + [
            L for L in leaders[1:]
            if top_sector_score > 0
            and float(L.get("sector_score", 0) or 0) >= top_sector_score * ratio
        ]
        band_leaders = band_leaders[:max_sectors]
        if band_leaders:
            lines.append("")
            _own_desc = "겹침=점유락(먼저 잡는 봇)" if own_priority else "스톡봇 종목 제외"
            lines.append(f"**🧮 매매 바스켓** (섹터·종목 {ratio*100:.0f}% 룰 · {_own_desc})")
            for si, SL in enumerate(band_leaders, 1):
                top3 = sorted((SL.get("top3") or []), key=lambda x: x.get("rank", 9))
                if not top3:
                    continue
                lines.append(f"　**{si}. {SL.get('sector', '')}**")
                # 점수 기반 바스켓 룰: 2·3등의 stock_score가 1등의 ratio% 이상이어야 포함.
                # stock_score 없는 구버전 picks 호환: change_pct 기반 폴백.
                lead_sc = float(top3[0].get("stock_score", 0))
                lead_chg = float(top3[0].get("change_pct", 0))
                use_score = lead_sc > 0
                thresh_sc = lead_sc * ratio
                thresh_chg = lead_chg * ratio
                for m in top3:
                    code = _b6(m.get("code"))
                    chg = float(m.get("change_pct", 0))
                    sc = float(m.get("stock_score", 0))
                    nm = m.get("name", "")
                    # 선별 조건값 — 시총은 이미 통과 전제라 생략, 거래대금·평소대비만 표시.
                    val_eok = float(m.get("value_won", 0) or 0) / 1e8
                    vr = float(m.get("vol_ratio", 0) or 0)
                    cond = f"{chg:+.1f}% · 거래대금{val_eok:,.0f}억 · 평소×{vr:.1f}"
                    if m.get("rank", 1) >= 2:
                        below = (sc < thresh_sc) if use_score else (chg < thresh_chg)
                        if below:
                            _base = (f"점수 {to_display_stock(sc):.1f}점 (기준 {to_display_stock(thresh_sc):.1f}점)"
                                     if use_score else f"{chg:+.1f}% (기준 {thresh_chg:+.1f}%)")
                            lines.append(f"　　❌ {nm}({code}) {cond} — {ratio*100:.0f}%룰 미달({_base})")
                            continue
                    if code in own:
                        if own_priority:
                            lines.append(f"　　⚖️ {nm}({code}) {cond} — 스톡봇과 겹침(점유락: 먼저 잡는 봇)")
                        else:
                            lines.append(f"　　❌ {nm}({code}) {cond} — 스톡봇 보유종목")
                    else:
                        sc_tag = f" [점수:{to_display_stock(sc):.1f}점]" if use_score else ""
                        lines.append(f"　　✅ {nm}({code}) {cond}{sc_tag}")
    else:
        lines.append("⚠️ 조건 충족 대장주 없음")
        # 진단 요약 — 왜 없는지(회전율/거래대금/등락률 하한 등) 한눈에.
        _dg = res.get("diag") or {}
        if _dg:
            lines.append("```" + "\n" + criteria_text(_dg) + "\n" + "```")
            _ql = _dg.get("qualified") or []
            if _ql:
                lines.append("**자격통과 종목(점수순 상위)**")
                for _q in _ql[:5]:
                    _mc = _q.get("mktcap_eok", 0)
                    _mc_s = f" · 시총{_mc/1e4:.1f}조" if _mc >= 1e4 else (f" · 시총{_mc:,.0f}억" if _mc > 0 else "")
                    lines.append(
                        f"　• [{_q['name'][:10]}] {_q['change_pct']:+.2f}% · "
                        f"{_q['value_eok']:,.0f}억(평소×{_q.get('vol_ratio',0):.1f}) · "
                        f"회전{_q['turnover_pct']:.2f}%{_mc_s} · "
                        f"점수 {to_display_stock(_q['stock_score']):.1f}점 [{_q['sector']}]"
                    )
            _nr = _dg.get("near") or []
            if _nr:
                lines.append("**아깝게 탈락 (2~3개 통과, 최대 10개)**")
                for _n in _nr[:10]:
                    _fails = _n.get("fails") or ([_n.get("fail")] if _n.get("fail") else [])
                    _reason = ", ".join(_fails)
                    lines.append(
                        f"　• [{_n['name'][:10]}] 통과 {_n.get('passes', 0)}/4 · "
                        f"{_n['change_pct']:+.2f}% · {_n['value_eok']:,.0f}억 "
                        f"[{_n['sector']}] ← {_reason}"
                    )
            if _dg.get("reason"):
                lines.append(f"_사유: {_dg['reason']}_")

    if hot:
        lines.append("")
        lines.append("**🔥 핫섹터**")
        for s in hot[:6]:
            lines.append(
                f"• {s['sector']}  상승 {s['riser_count']}종목  "
                f"Top3평균 {s['avg_change']:+.1f}%  "
                f"Top3거래대금 {s['total_value']/1e8:.0f}억"
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
_MKTCAP_CACHE_PATH = _CACHE_DIR / "leader_mktcap_cache.json"


# 재크롤 결과가 직전 캐시의 이 비율에 못 미치면 '부분 크롤'로 보고 채택하지 않는다.
_MKTCAP_MIN_RATIO = 0.8


def _load_mktcap_cache() -> dict[str, float]:
    """당일 매경 시총 캐시 로드. 날짜 mismatch/누락/파싱실패 시 매경 재크롤링.

    반환: {code: market_cap_won}. 실패 시 빈 dict (leader_finder 는 mktcap==0
    시 게이트 pass 하므로 blackout 아님).

    2026-08-19: 하루 묵은 캐시를 폴백으로 남긴다. 이 값의 유일한 용도가
    '시총 ≥ min_cap' 하한 필터라 하루치 등락으로 판정이 뒤집히는 건 경계선의
    극소수인데, 재크롤은 페이지 하나만 실패해도 수백 종목이 통째로 빠진다
    (실측 08-19: kosdaq p18 DNS 실패 → 2494 → 1668종목). '신선하지만 반쪽'
    보다 '하루 묵었지만 온전한' 쪽이 유니버스에 안전하다. 부분 크롤은 저장도
    하지 않아 다음 호출이 다시 시도한다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    stale: dict[str, float] = {}
    try:
        if _MKTCAP_CACHE_PATH.exists():
            data = json.loads(_MKTCAP_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data.get("caps"), dict):
                caps0 = {k: float(v) for k, v in data["caps"].items()}
                if data.get("date") == today:
                    return caps0
                stale = caps0  # 어제(이전) 캐시 — 재크롤 실패 시 폴백
    except Exception as e:
        print(f"  [시총 캐시 로드 실패] {e}")
    # 캐시 miss → 매경 재크롤링
    try:
        caps = mk_quant.fetch_marketcap_map()
        incomplete = bool(getattr(mk_quant, "LAST_MKTCAP_INCOMPLETE", False))
        short = bool(stale) and len(caps) < len(stale) * _MKTCAP_MIN_RATIO
        # 부분 크롤이면 30초 쉬고 1회 더 — 페이지 한 장 DNS/타임아웃 실패로
        # 저장을 건너뛰면 그날 픽(09:30)이 재크롤 비용(~24초)을 대신 문다
        # (실측 2026-08-20: 02시 부분크롤 → 폴백 → 09:30 시총주입 23.8초).
        if caps and (incomplete or short):
            print(f"  [시총 캐시] 부분 크롤 {len(caps)}종목 — 30초 후 1회 재시도")
            time.sleep(30)
            caps2 = mk_quant.fetch_marketcap_map()
            inc2 = bool(getattr(mk_quant, "LAST_MKTCAP_INCOMPLETE", False))
            short2 = bool(stale) and len(caps2) < len(stale) * _MKTCAP_MIN_RATIO
            if caps2 and not inc2 and not short2:
                caps, incomplete, short = caps2, inc2, short2
            elif len(caps2) > len(caps):
                caps = caps2  # 여전히 반쪽이지만 더 온전한 쪽을 남긴다
        if caps and not incomplete and not short:
            _MKTCAP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _MKTCAP_CACHE_PATH.write_text(
                json.dumps({"date": today, "caps": caps}, ensure_ascii=False),
                encoding="utf-8")
            print(f"  [시총 캐시] 매경 신규 크롤링 {len(caps)}종목 저장")
            return caps
        if caps:
            why = "페이지 유실" if incomplete else f"직전 대비 {len(caps)}/{len(stale)}"
            print(f"  [시총 캐시] 부분 크롤 감지({why}) — 저장 안 함")
            if stale:
                print(f"  [시총 캐시] 직전 캐시 {len(stale)}종목으로 폴백")
                return stale
            return caps  # 폴백 없음 → 반쪽이라도 쓰되 캐시엔 남기지 않는다
    except Exception as e:
        print(f"  [시총 캐시 재크롤링 실패] {e}")
    if stale:
        print(f"  [시총 캐시] 재크롤 실패 — 직전 캐시 {len(stale)}종목으로 폴백")
    return stale


def prefetch_avgval(fetch_n: int = 600, min_cap_eok: float = 1000.0,
                    pace_sec: float = 1.0) -> None:
    """새벽 02시 크론용 avg_value_nd(5거래일 평균 거래대금) 프리페치.

    09:30 첫 선별 tick 의 KIS 호출 병목(캐시미스 종목 × 순차)을 새벽으로 밀어
    낮 시간 부하 0. 새벽이라 시간 여유가 크므로 상한을 두지 않고 시총 조건을
    만족하는 종목을 전부 프리페치한다. 로직:
      ① 시총 유니버스 정의: 매경 시총 캐시에서 시총≥min_cap 인 종목 집합 전부
      ② 그 종목 전부에 대해 KIS KRX 일봉으로 avg_value_nd 를 순차 호출해 저장
    다움 거래대금 랭킹으로 교집합을 걸던 예전 방식은 폐기(2026-08-15) — 그날
    거래대금 순위가 낮아 다움 상위권 밖이던 종목이 통째로 누락되는 문제가 있어,
    새벽 시간 여유를 살려 시총 조건만으로 전수 프리페치한다.
    09:30 tick 은 avg_value_nd() 첫 라인에서 오늘자 캐시 히트 → 즉시 반환.

    Args:
      fetch_n:     미사용(과거 다움 랭킹 조회 개수 — 하위호환용 시그니처 유지).
      min_cap_eok: 시가총액 하한 억원. 유니버스 정의에 사용 (기본 1000).
      pace_sec:    KIS 호출 간격 초 (모의 1건/초 유량 준수).
    """
    t0 = time.time()
    # 기존 캐시 로드(과거 date·다른 창(w)의 엔트리는 avg_value_nd 이 자동 무시)
    _load_avgval_cache()
    # ① 시총 유니버스 — 매경 시총 캐시 로드(캐시 miss → 매경 재크롤). 02시엔 어제 마감 시총.
    caps = _load_mktcap_cache()
    min_cap_won = float(min_cap_eok) * 1e8
    if not caps:
        print("[prefetch_avgval] 시총 캐시 로드 실패 — 종료")
        return
    codes = [c for c, v in caps.items() if float(v) >= min_cap_won]
    print(f"[prefetch_avgval] 시총 ≥ {min_cap_eok:g}억 유니버스 {len(codes)}종목 "
          f"(전체 시총 캐시 {len(caps)}) — 전수 프리페치 대상")
    # 4) 오늘자 이미 캐시된 종목은 건너뛰기(재실행 안전성)
    today = datetime.now().strftime("%Y%m%d")
    # 창(w)이 현행과 다른 엔트리도 재계산 대상 — 5일창 잔재를 하루 만에 걷어낸다.
    todo = [c for c in codes if not (_AVGVAL_CACHE.get(c, {}).get("date") == today
                                     and _AVGVAL_CACHE.get(c, {}).get("avg", 0) > 0
                                     and int(_AVGVAL_CACHE.get(c, {}).get("w", 5))
                                     == AVGVAL_WINDOW_D)]
    hit = len(codes) - len(todo)
    if hit:
        print(f"[prefetch_avgval] 오늘자 캐시 hit {hit} — 재계산 스킵")
    # 5) 순차 호출 (모의 1건/초 준수)
    ok = fail = 0
    save_every = 50  # 50건마다 중간 저장(중단 대비)
    for i, code in enumerate(todo, 1):
        try:
            avg = avg_value_nd(code)
        except Exception as e:
            avg = 0.0
            print(f"[prefetch_avgval] {code} 예외: {e}")
        if avg > 0:
            ok += 1
        else:
            fail += 1
        if i % 50 == 0:
            elapsed = time.time() - t0
            print(f"[prefetch_avgval] {i}/{len(todo)} 진행 (성공 {ok}·실패 {fail}) "
                  f"경과 {elapsed:.0f}초")
        if i % save_every == 0:
            _save_avgval_cache()
        # KIS 유량 준수(호출 자체가 ~0.1~0.3초 걸리므로 실제 간격은 pace_sec 근사)
        if pace_sec > 0 and i < len(todo):
            time.sleep(pace_sec)
    _save_avgval_cache()
    elapsed = time.time() - t0
    print(f"[prefetch_avgval] 완료: 유니버스 {len(codes)} · 오늘자캐시 {hit} 제외 · "
          f"대상 {len(todo)} · 성공 {ok} · 실패 {fail} · "
          f"소요 {elapsed:.0f}초")
    # ── 업종 프리페치: 같은 시총 유니버스로 이어서 (2026-08-19) ──────────
    # 09:30 선별의 잔여 병목(유니버스 200종목 업종 2단 크롤 ~70초) 제거.
    # 업종은 영구값이라 콜드 1회 이후엔 신규 상장분만 조회된다.
    try:
        prefetch_sectors(codes)
    except Exception as e:
        print(f"[prefetch_sectors] 실패(무해, 선별이 직접 조회): {e}")


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
        # 그날 실제 적용된 선별 조건·funnel 요약(문자열). leader_trader 가 바스켓
        # 로그에 같이 찍는다 — 거래대금 하한은 동적배수로 매일 달라져서
        # 결과(바스켓)만 봐서는 어떤 기준으로 나온 건지 알 수 없었다.
        "criteria": criteria_text(res.get("diag") or {}),
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
             # 100점 표시 환산(display-only) — 선별/밴드룰/정렬은 위 raw 값만 쓴다.
             "sector_score_100": to_display_sector(float(L.get("sector_score", 0) or 0)),
             "stock_score_100": to_display_stock(float(L.get("stock_score", 0) or 0)),
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
    _stage_reset()
    frac = _session_fraction(start_dt)
    # 1순위 다음(daum_quant) — 실시간 KRX 거래대금 API, ETF 제외 후 시장당 top_n 채우기.
    #   (매경은 SSR 캐시 지연으로 장중 순위가 뒤처져 2026-08-11 교체)
    # 2순위 KIS KRX(kis_quant) — 다음 실패 시 폴백. NXT/UN 재조회 없음.
    try:
        rank_df = daum_quant.fetch_ranking(top_n=args.top, stock_only=not args.include_etf)
    except Exception as e:
        print(f"  [다음 실패 → KIS 폴백] {e}")
        rank_df = pd.DataFrame()
    if rank_df is None or rank_df.empty:
        print("  [폴백] 다음 빈 결과 → KIS KRX 거래대금 사용")
        try:
            rank_df = kis_quant.fetch_ranking(
                top_n=args.top, stock_only=not args.include_etf,
                min_value=args.min_value * 1e8)
        except Exception as e:
            print(f"  [KIS 실패] {e}")
            rank_df = pd.DataFrame()
    _stage("랭킹수집")
    if rank_df.empty:
        print("  [경고] 순위 데이터 수집 실패")
        return
    # ── 시가총액 주입 (2026-08-10) ────────────────────────────────────────
    # 매경 거래대금 페이지에는 시총이 없어 매경 시총 랭킹을 장 시작 전 1회 캐시.
    # rank_df.market_cap == 0 인 종목만 캐시값으로 채운다 (KIS 폴백은 자체 시총 보유).
    caps = _load_mktcap_cache()
    if caps and "market_cap" in rank_df.columns:
        need = rank_df["market_cap"].fillna(0) <= 0
        need_n = int(need.sum())
        rank_df.loc[need, "market_cap"] = rank_df.loc[need, "code"].map(caps).fillna(0.0)
        filled = int((rank_df.loc[need, "market_cap"] > 0).sum())
        still_zero = int((rank_df["market_cap"].fillna(0) <= 0).sum())
        total = len(rank_df)
        matched = total - still_zero
        rate = (matched / total * 100.0) if total else 0.0
        print(f"  [시총 주입] 캐시 {len(caps)}종목 · 필요 {need_n}건→채움 {filled} · "
              f"최종 매칭 {matched}/{total} ({rate:.1f}%) · 시총0 잔여 {still_zero}")
    _stage("시총주입")
    # ── §2 동적 거래대금 임계값(토글, 기본 OFF) ──────────────────────────────
    # dyn_value_pct>0 이면 고정 min_value(억) 대신 '유니버스(코스피+코스닥 통합
    # 상위) 거래대금 합 × pct%'를 종목별 거래대금 하한으로 사용 → 장중 활황도에
    # 비례해 자동 조정. 0(기본)이면 고정값 그대로 → 동작 불변. 다른 게이트·선별
    # 로직은 무변경, 거래대금 하한값만 대체한다.
    eff_min_value = args.min_value * 1e8
    min_value_by_market: dict[str, float] | None = None
    dyn_pct = float(getattr(args, "dyn_value_pct", 0.0) or 0.0)
    # 시장별 top-N 합을 별도 기록 → 시장별로 배수 계산(코스피/코스닥 활황 사이클 분리).
    today_base_sum = float(rank_df["value_won"].sum())
    _mkt_col = rank_df["market"].astype(str).str.upper() if "market" in rank_df.columns else pd.Series([], dtype=str)
    today_kospi = float(rank_df.loc[_mkt_col == "KOSPI", "value_won"].sum()) if len(_mkt_col) else 0.0
    today_kosdaq = float(rank_df.loc[_mkt_col == "KOSDAQ", "value_won"].sum()) if len(_mkt_col) else 0.0
    today_key = start_dt.strftime("%Y%m%d")
    _record_market_flow(today_key, today_kospi, today_kosdaq)
    slot = _slot_key(start_dt)
    _value_source = ""  # diag 노출용 — market_flow 배수 실제 적용 여부 진단
    if dyn_pct > 0:
        # legacy §2 방식(오늘 유니버스합 × pct%) — 시각비례 없음. 탈출구.
        eff_min_value = today_base_sum * dyn_pct / 100.0
        _value_source = f"dyn_legacy: 유니버스{today_base_sum/1e8:,.0f}억×{dyn_pct:g}% = {eff_min_value/1e8:,.0f}억"
        print(f"  [동적 거래대금·legacy] 유니버스합 {today_base_sum/1e8:,.0f}억 × {dyn_pct:g}% "
              f"= {eff_min_value/1e8:,.0f}억 (고정 {args.min_value:.0f}억 대체)")
    else:
        mf_low   = float(getattr(args, "mf_clamp_low", 0.5))
        mf_high  = float(getattr(args, "mf_clamp_high", 1.5))
        mults, id_diag = _compute_intraday_flow_multiplier(
            today_key, slot, today_kospi, today_kosdaq, frac, mf_low, mf_high
        )
        # 시간비례 스케일: pick_frac(now) / pick_frac(anchor) — pick 창(09:00~pick_end) 기준.
        # 앵커: anchor_hhmm 시각에서 anchor_value 억이 목표 하한.
        # 최종: clip(anchor × (pick/pick_anchor) × mult, floor, cap) — floor/cap 은 absolute policy.
        anchor_hhmm = str(getattr(args, "min_value_anchor_hhmm", "11:00") or "11:00")
        pick_end    = str(getattr(args, "pick_window_end", "13:00") or "13:00")
        floor_eok   = float(getattr(args, "min_value_floor", 150.0) or 0.0)
        cap_eok     = float(getattr(args, "max_value", 800.0) or 0.0)
        pick_now    = _pick_fraction(start_dt, pick_end)
        # anchor 자체의 pick_frac 계산 — 오늘 날짜의 anchor 시각 datetime 만들어 재사용
        try:
            ah, am = anchor_hhmm.split(":")
            anchor_dt = start_dt.replace(hour=int(ah), minute=int(am), second=0, microsecond=0)
        except (ValueError, AttributeError):
            anchor_dt = start_dt.replace(hour=11, minute=0, second=0, microsecond=0)
        pick_anchor = _pick_fraction(anchor_dt, pick_end)
        pick_anchor = max(pick_anchor, 0.02)  # 0 분모 방어
        time_scale = pick_now / pick_anchor
        anchor_val = args.min_value * 1e8
        raw_base   = anchor_val * time_scale
        floor_v    = floor_eok * 1e8
        cap_v      = cap_eok * 1e8 if cap_eok > 0 else float("inf")
        def _clip(x: float) -> float:
            return max(floor_v, min(cap_v, x))
        min_value_by_market = {
            "KOSPI":  _clip(raw_base * float(mults.get("kospi", 1.0))),
            "KOSDAQ": _clip(raw_base * float(mults.get("kosdaq", 1.0))),
        }
        eff_min_value = min(min_value_by_market.values())  # 진단·폴백용
        _value_source = (
            f"anchor {anchor_val/1e8:,.0f}억@{anchor_hhmm} × time_scale {time_scale:.3f} "
            f"(pick {pick_now:.3f}/{pick_anchor:.3f}) → raw {raw_base/1e8:,.0f}억 · "
            f"KOSPI×{mults['kospi']:.3f}→{min_value_by_market['KOSPI']/1e8:,.0f}억 · "
            f"KOSDAQ×{mults['kosdaq']:.3f}→{min_value_by_market['KOSDAQ']/1e8:,.0f}억 "
            f"[floor {floor_eok:g}억 · cap {cap_eok:g}억] ({id_diag})"
        )
        print(f"  [intraday_flow] {id_diag}")
        print(f"  [시간비례] anchor {anchor_val/1e8:,.0f}억@{anchor_hhmm} × pick {pick_now:.3f}/{pick_anchor:.3f}"
              f" = raw {raw_base/1e8:,.0f}억 → clip[{floor_eok:g}, {cap_eok:g}]")
        print(f"    → 하한 KOSPI {min_value_by_market['KOSPI']/1e8:,.0f}억 · "
              f"KOSDAQ {min_value_by_market['KOSDAQ']/1e8:,.0f}억")
    _to_cap = float(getattr(args, "turnover_cap_pct", 200.0))
    # 실전은 항상 네이버 테마 모드. by-sector 모드는 폐기됨(2026-08).
    res = find_leaders_by_theme(rank_df, args.vol_mult, frac,
                                min_value=eff_min_value,
                                min_value_by_market=min_value_by_market,
                                min_mktcap=args.min_mktcap * 1e8,
                                max_change=args.max_change,
                                theme_min_change=args.theme_min_change,
                                rise_min=args.rise_min,
                                hot_min=args.hot_min,
                                turnover_cap_pct=_to_cap)
    _stage("선정")
    if isinstance(res.get("diag"), dict):
        res["diag"]["value_source"] = _value_source
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
    _save_theme_cache()   # 프리페치가 못 돈 날에도 첫 회차 크롤을 재시도 회차가 재사용
    _save_sector_cache()  # 신규 상장/유니버스 밖 종목의 업종도 그때그때 영구 적재
    _stage("출력·저장")
    _rep = _stage_report()
    if _rep:
        print(_rep)


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
    ap.add_argument("--min-value", type=float, default=500.0,
                    help="거래대금 앵커값(억원, 기본 500). anchor_hhmm 시각에서의 목표 하한. "
                         "실제 하한 = clip(anchor × pick_frac(now)/pick_frac(anchor) × mult, floor, cap).")
    ap.add_argument("--min-value-anchor-hhmm", type=str, default="11:00",
                    help="시간비례 앵커 시각 HH:MM (기본 11:00). pick_frac(11:00)=0.5.")
    ap.add_argument("--max-value", type=float, default=800.0,
                    help="거래대금 하한 cap(억원, 기본 800). mult·시간비례 적용 후 이 값 초과 방지.")
    ap.add_argument("--min-value-floor", type=float, default=150.0,
                    help="거래대금 하한 floor(억원, 기본 150). mult·시간비례 적용 후 이 값 아래로 안 감.")
    ap.add_argument("--pick-window-end", type=str, default="13:00",
                    help="pick 창 종료 시각 HH:MM (기본 13:00, LEADER_SWITCH_UNTIL). pick_frac 분모.")
    ap.add_argument("--dyn-value-pct", type=float, default=0.0,
                    help="§2 동적 거래대금(legacy): 유니버스 거래대금 합의 N%%를 종목별 하한으로 사용. "
                         "0(기본)=미적용, market_flow 배수 사용. >0이면 market_flow 무시하고 legacy 우선.")
    ap.add_argument("--mf-clamp-low", type=float, default=0.5,
                    help="intraday_flow: 배수 하한(조용한 날 방어). 기본 0.5")
    ap.add_argument("--mf-clamp-high", type=float, default=2.0,
                    help="intraday_flow: 배수 상한(활황기 방어). 기본 2.0")
    ap.add_argument("--min-mktcap", type=float, default=1000.0, help="시가총액 최소 (억원, 기본 1000)")
    ap.add_argument("--max-change", type=float, default=25.0,
                    help="등락률 상한 %% — 과열주 제외 (기본 25). 상한가30%%-익절4%%-여유1%%: "
                         "진입 후 +4%% 익절 여력 없는 과열주는 대장주 후보에서 제외")
    # 회전율: 게이트 폐지(2026-08-11), 값은 섹터/종목강도 스코어링에만 사용.
    ap.add_argument("--turnover-cap-pct", type=float, default=200.0,
                    help="회전율 pctile 입력값 극단치 캡(%%). 0=무제한. (기본 200)")
    ap.add_argument("--once", action="store_true", help="대기 없이 지금 즉시 1회(테스트)")
    ap.add_argument("--include-etf", action="store_true", help="ETF/ETN 포함(기본 제외)")
    ap.add_argument("--ignore-hours", action="store_true", help="장시간 무시하고 실행")
    ap.add_argument("--theme", action="store_true", help="[하위호환 no-op] 항상 테마 모드로 동작")
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
    ap.add_argument("--prefetch-market-flow", action="store_true",
                    help="pykrx KRX-only 로 최근 20영업일 top-N 거래대금 합을 캐시 "
                         "(leader_market_flow.v2.json) 에 백필/갱신하고 종료. 장전 08:30 크론용.")
    ap.add_argument("--prefetch-avgval", action="store_true",
                    help="새벽 02시 크론 전용: 매경 시총 캐시에서 시총≥min-cap 인 종목 전부(상한 없음) "
                         "avg_value_nd(5거래일 평균)를 KIS KRX 로 순차 프리페치해 "
                         "디스크 캐시에 저장. "
                         "09:30 첫 pick tick 의 KIS 병목 제거용. (다움 교집합 방식은 2026-08-15 폐기)")
    ap.add_argument("--prefetch-sectors", action="store_true",
                    help="업종 캐시만 단독 프리페치. 정상 운영에서는 02:00 "
                         "--prefetch-avgval 이 같은 유니버스로 이어서 실행하고, "
                         "러너의 비영업일 부팅 캐시 백필이 이 경로를 쓴다.")
    ap.add_argument("--prefetch-themes", action="store_true",
                    help="09:05 크론 전용: 네이버 테마 구성종목 전체를 크롤해 디스크 캐시"
                         "(leader_theme_cache.json, 날짜 키)에 저장하고 종료. 09:30 선별의 "
                         "최대 병목(테마 263개 재크롤 ~90초) 제거용.")
    ap.add_argument("--prefetch-fetch-n", type=int, default=600,
                    help="미사용(과거 다움 랭킹 조회 개수 — 하위호환용 시그니처 유지)")
    ap.add_argument("--prefetch-min-cap-eok", type=float, default=1000.0,
                    help="--prefetch-avgval: 시가총액 하한 억원 (기본 1000억)")
    ap.add_argument("--prefetch-pace-sec", type=float, default=1.0,
                    help="--prefetch-avgval: KIS 호출 간격 초 (기본 1.0, 모의 유량)")
    ap.add_argument("--cache-only", action="store_true",
                    help="마감 후(15:35 크론) 캐시 스냅샷 전용: rank_df 만 받아 "
                         "market_flow/intraday_flow 캐시에 오늘 close 값 기록 후 종료. "
                         "선별·매매·디스코드 알림 모두 생략.")
    args = ap.parse_args()

    if args.cache_only:
        start_dt = datetime.now()
        try:
            rank_df = fetch_ranking_unified(top_n=args.top, stock_only=not args.include_etf)
        except Exception as e:
            print(f"[cache-only] 네이버 unified 실패: {e}")
            return
        if rank_df is None or rank_df.empty:
            print("[cache-only] rank_df 빈 결과 — 스킵")
            return
        # NXT 백필(threshold 근접 편측관측만) — run_once 와 동일 로직
        if "src_krx" in rank_df.columns and "src_nxt" in rank_df.columns:
            min_v = args.min_value * 1e8
            edge_mask = (~(rank_df["src_krx"] & rank_df["src_nxt"])) & \
                        (rank_df["value_won"] >= min_v * 0.3)
            missing = rank_df[edge_mask]
            if not missing.empty:
                broker = _get_leader_broker()
                if broker is not None:
                    try:
                        from kis_quant import _un_quote
                        updated = 0
                        for idx, r in missing.iterrows():
                            try:
                                un_val, _p, _pr = _un_quote(broker, r["code"])
                            except Exception:
                                un_val = 0.0
                            if un_val > rank_df.at[idx, "value_won"]:
                                rank_df.at[idx, "value_won"] = un_val
                                updated += 1
                        print(f"[cache-only] NXT 백필 {len(missing)}건 중 {updated} 교체")
                    except Exception as e:
                        print(f"[cache-only] NXT 백필 실패: {e}")
        today_base_sum = float(rank_df["value_won"].sum())
        _mkt_col2 = rank_df["market"].astype(str).str.upper() if "market" in rank_df.columns else pd.Series([], dtype=str)
        t_k = float(rank_df.loc[_mkt_col2 == "KOSPI", "value_won"].sum()) if len(_mkt_col2) else 0.0
        t_q = float(rank_df.loc[_mkt_col2 == "KOSDAQ", "value_won"].sum()) if len(_mkt_col2) else 0.0
        today_key = start_dt.strftime("%Y%m%d")
        _record_market_flow(today_key, t_k, t_q)
        print(f"[cache-only] {today_key} {_slot_key(start_dt)} "
              f"KOSPI={t_k/1e12:.2f}조 KOSDAQ={t_q/1e12:.2f}조 (합 {today_base_sum/1e12:.2f}조) 캐시 기록 완료")
        return

    if args.prefetch_market_flow:
        added, total, msg = prefetch_market_flow(
            days=MF_WINDOW_D,
            top_n=int(args.top),
        )
        print(f"[prefetch_market_flow] {msg}")
        return

    if args.prefetch_themes:
        prefetch_themes()
        return

    if args.prefetch_sectors:
        _caps = _load_mktcap_cache()
        _min = float(args.prefetch_min_cap_eok) * 1e8
        prefetch_sectors([c for c, v in _caps.items() if float(v) >= _min])
        return

    if args.prefetch_avgval:
        prefetch_avgval(
            fetch_n=int(args.prefetch_fetch_n),
            min_cap_eok=float(args.prefetch_min_cap_eok),
            pace_sec=float(args.prefetch_pace_sec),
        )
        return

    _load_avgval_cache()
    _load_trend_cache()
    _load_theme_cache()
    _load_sector_cache()
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
