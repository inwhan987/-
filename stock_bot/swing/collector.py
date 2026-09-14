# -*- coding: utf-8 -*-
"""일별 데이터 수집기 (명세 4절, 13절 2단계).

소스(2026-09-14 test_kis_data.py 실측):
  일봉      KIS inquire-daily-itemchartprice  100행/호출, FID_INPUT_DATE_2 로 뒤로 페이징
  수급      KIS inquire-investor              최근 30영업일만 (과거분은 pykrx 날짜 루프)
  밸류      KIS inquire-price / itemchartprice output1  당일치만 (과거분은 pykrx 날짜 루프)
  프로그램  KIS program-trade-by-stock-daily  30행/호출, 뒤로 페이징
  지수      KIS inquire-daily-indexchartprice 50행/호출, 뒤로 페이징 → daily 에 IDX0001
  유니버스  pykrx get_market_ticker_list(ALL) + 종목명

단위 통일(원): KIS *_ntby_tr_pbmn 은 백만원 → ×1e6, hts_avls 는 억원 → ×1e8.
pykrx 는 원 단위 그대로. 둘을 섞어 쓰는 flow 테이블은 항상 원.

실패 정책(명세 11절): 종목 단위 실패는 건너뛰고 기록, 전체의 20% 이상 실패면
Blocked 를 던져 배치를 멈춘다(우회하지 않는다).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
import pandas as pd
from loguru import logger

from stock_bot.broker.kis import KISBroker, TOKEN_CACHE_DIR
from stock_bot.config.settings import settings

from . import store
from .config import ROOT, cfg

IDX_CODE = "IDX0001"          # KOSPI — daily 테이블에 종목처럼 넣는다
_MAX_FAIL_RATIO = 0.20


class Blocked(RuntimeError):
    """수집 실패율이 한도를 넘음 — 배치 중단."""


# ── KIS 데이터 전용 브로커 ────────────────────────────────────────────
class DataBroker(KISBroker):
    """시세 조회 전용 KISBroker.

    SWING_DATA_KIS_ENV=real 이면 .env 의 KIS_DATA_APP_KEY/SECRET(별도 실전 키)로
    실전 서버(18/s)를 쓴다. 키가 없으면 우회하지 않고 즉시 중단한다 — 주문용 실전
    키로 토큰을 새로 받으면 파이의 토큰이 무효화되기 때문.
    paper 는 기존 모의 키(1/s)를 그대로 쓴다.
    """

    def __init__(self, env: str | None = None, rps: float | None = None) -> None:
        env = env or cfg().data_kis_env
        super().__init__()
        self.env = env
        if env == "real":
            key = os.environ.get("KIS_DATA_APP_KEY", "").strip()
            sec = os.environ.get("KIS_DATA_APP_SECRET", "").strip()
            if not key or not sec:
                raise SystemExit(
                    "SWING_DATA_KIS_ENV=real 인데 KIS_DATA_APP_KEY / KIS_DATA_APP_SECRET 이 "
                    ".env 에 없다. 별도 실전 키를 넣거나 paper 로 바꿔라.")
            self.base_url = "https://openapi.koreainvestment.com:9443"
            self.app_key, self.app_secret = key, sec
            self._rps = rps or 15.0
        else:
            self.base_url = settings.kis_base_url if settings.is_paper else \
                "https://openapivts.koreainvestment.com:29443"
            self._rps = rps or 1.0
        self._client.close()
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)
        self._token = None
        self._token_expires_at = 0.0
        self._last_ts = 0.0

    @property
    def _token_cache_path(self) -> Path:
        return TOKEN_CACHE_DIR / ("data_real.json" if self.env == "real" else "paper.json")

    def _throttle(self, priority: bool = False) -> None:  # noqa: ARG002
        wait = self._last_ts + 1.0 / self._rps - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_ts = time.monotonic()

    def get_json(self, path: str, tr_id: str, params: dict[str, Any], label: str = "") -> dict:
        r = self._get_with_retry(path, tr_id, params, label=label, attempts=4)
        j = r.json()
        if str(j.get("rt_cd", "0")) != "0":
            raise RuntimeError(f"{label or tr_id}: {j.get('msg_cd')} {j.get('msg1')}")
        return j


def _f(v: Any) -> float | None:
    try:
        s = str(v).strip().replace(",", "")
        return float(s) if s not in ("", "-") else None
    except Exception:
        return None


def _prev_day(d: str) -> str:
    return (datetime.strptime(d, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


# ── 개별 페치 ─────────────────────────────────────────────────────────
def fetch_daily(b: DataBroker, code: str, start: str, end: str,
                max_pages: int = 20) -> tuple[list[dict], dict]:
    """일봉 [start, end] 를 100행씩 뒤로 페이징. (rows, meta) — meta 는 output1 당일치."""
    rows: list[dict] = []
    meta: dict = {}
    cur_end = end
    for _ in range(max_pages):
        j = b.get_json("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                       "FHKST03010100",
                       {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                        "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": cur_end,
                        "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
                       label=f"daily {code}")
        if not meta:
            o1 = j.get("output1") or {}
            meta = {"name": (o1.get("hts_kor_isnm") or "").strip(),
                    "per": _f(o1.get("per")), "pbr": _f(o1.get("pbr")),
                    "eps": _f(o1.get("eps")),
                    "shares": _f(o1.get("lstn_stcn")),
                    "mktcap": (_f(o1.get("hts_avls")) or 0) * 1e8 or None}
        page = [r for r in (j.get("output2") or []) if r.get("stck_bsop_date") and r.get("stck_clpr")]
        if not page:
            break
        for r in page:
            rows.append({"code": code, "date": r["stck_bsop_date"],
                         "open": _f(r.get("stck_oprc")), "high": _f(r.get("stck_hgpr")),
                         "low": _f(r.get("stck_lwpr")), "close": _f(r.get("stck_clpr")),
                         "volume": int(_f(r.get("acml_vol")) or 0),
                         "value": int(_f(r.get("acml_tr_pbmn")) or 0)})
        oldest = page[-1]["stck_bsop_date"]
        if len(page) < 100 or oldest <= start:
            break
        cur_end = _prev_day(oldest)
    rows.sort(key=lambda r: r["date"])
    return rows, meta


def fetch_investor(b: DataBroker, code: str) -> list[dict]:
    """투자자별 순매수 대금(원). 최근 30영업일만 온다. 당일 행은 15:30 이후 채워짐."""
    j = b.get_json("/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900",
                   {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
                   label=f"investor {code}")
    out = []
    for r in j.get("output") or []:
        d = r.get("stck_bsop_date")
        if not d or r.get("frgn_ntby_tr_pbmn") in (None, ""):
            continue
        out.append({"code": code, "date": d,
                    "forgn": (_f(r.get("frgn_ntby_tr_pbmn")) or 0) * 1e6,
                    "inst": (_f(r.get("orgn_ntby_tr_pbmn")) or 0) * 1e6,
                    "indiv": (_f(r.get("prsn_ntby_tr_pbmn")) or 0) * 1e6})
    return out


def fetch_program(b: DataBroker, code: str, start: str, end: str,
                  max_pages: int = 20) -> list[dict]:
    """프로그램매매 일별 순매수(수량·대금 원). 30행/호출, FID_INPUT_DATE_1 로 뒤로 페이징."""
    out: list[dict] = []
    cur = end
    for _ in range(max_pages):
        j = b.get_json("/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily",
                       "FHPPG04650200",
                       {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                        "FID_INPUT_DATE_1": cur},
                       label=f"program {code}")
        page = [r for r in (j.get("output") or []) if r.get("stck_bsop_date")]
        if not page:
            break
        for r in page:
            if r["stck_bsop_date"] < start:
                continue
            out.append({"code": code, "date": r["stck_bsop_date"],
                        "ntby_qty": _f(r.get("whol_smtn_ntby_qty")),
                        "ntby_value": _f(r.get("whol_smtn_ntby_tr_pbmn"))})
        oldest = min(r["stck_bsop_date"] for r in page)
        if oldest <= start or len(page) < 30:
            break
        cur = _prev_day(oldest)
    return out


def fetch_price_meta(b: DataBroker, code: str) -> dict:
    """당일 PER/PBR/EPS·시총·상장주식수 (inquire-price)."""
    j = b.get_json("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                   {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
                   label=f"price {code}")
    o = j.get("output") or {}
    return {"per": _f(o.get("per")), "pbr": _f(o.get("pbr")), "eps": _f(o.get("eps")),
            "shares": _f(o.get("lstn_stcn")),
            "mktcap": (_f(o.get("hts_avls")) or 0) * 1e8 or None,
            "close": _f(o.get("stck_prpr"))}


def fetch_index(b: DataBroker, start: str, end: str, iscd: str = "0001",
                max_pages: int = 20) -> list[dict]:
    """지수 일봉(KOSPI=0001). 50행/호출, 뒤로 페이징."""
    rows: list[dict] = []
    cur_end = end
    for _ in range(max_pages):
        j = b.get_json("/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
                       "FHKUP03500100",
                       {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd,
                        "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": cur_end,
                        "FID_PERIOD_DIV_CODE": "D"},
                       label="index")
        page = [r for r in (j.get("output2") or []) if r.get("stck_bsop_date") and r.get("bstp_nmix_prpr")]
        if not page:
            break
        for r in page:
            rows.append({"code": IDX_CODE, "date": r["stck_bsop_date"],
                         "open": _f(r.get("bstp_nmix_oprc")), "high": _f(r.get("bstp_nmix_hgpr")),
                         "low": _f(r.get("bstp_nmix_lwpr")), "close": _f(r.get("bstp_nmix_prpr")),
                         "volume": int(_f(r.get("acml_vol")) or 0),
                         "value": int(_f(r.get("acml_tr_pbmn")) or 0)})
        oldest = page[-1]["stck_bsop_date"]
        if len(page) < 50 or oldest <= start:
            break
        cur_end = _prev_day(oldest)
    rows.sort(key=lambda r: r["date"])
    return rows


# ── 유니버스 ──────────────────────────────────────────────────────────
def universe(date: str | None = None) -> list[dict]:
    """전 상장 종목 [{code,name,market}] (pykrx). 실패하면 예외 — 우회 없음."""
    from pykrx import stock  # noqa: WPS433
    d = date or datetime.now().strftime("%Y%m%d")
    out: list[dict] = []
    for mkt in ("KOSPI", "KOSDAQ"):
        codes = stock.get_market_ticker_list(d, market=mkt)
        time.sleep(1.5)
        for c in codes:
            out.append({"code": c, "market": mkt})
    if len(out) < 1000:
        raise RuntimeError(f"universe too small: {len(out)}")
    return out


# ── pykrx 과거분 (초기 적재 폴백) ─────────────────────────────────────
# 날짜 루프 구조. 종목 루프(2,483회×2콜, rps 0.7 → 13h)는 쓰지 않는다.
#   1) bt_swing 디스크 캐시(data/bt_swing_cache/{flow,fund}/{code}_{s}_{e}.pkl)가 덮는
#      구간은 파일에서만 읽는다 — 네트워크 0.
#   2) 그 밖의 영업일은 날짜 하나로 전종목을 주는 pykrx 함수로 받는다(실측 확인, 2025-09-05):
#        get_market_fundamental(d, market="ALL")                → 2,717행 BPS/PER/PBR/EPS/DIV/DPS
#        get_market_net_purchases_of_equities(d, d, "ALL", 투자자) → 투자자별 순매수거래대금(원)
#      forgn = 외국인 + 기타외국인 (= 종목별 함수의 '외국인합계' 와 원 단위까지 일치 확인),
#      inst = 기관합계, indiv = 개인. 표에 없는 종목 = 그날 그 투자자 거래 없음 → 0.
#      날짜당 5콜, 결과는 bydate/{d}_{kind}.pkl 에 캐시(실패는 캐시에 남기지 않음).
#   Blocked(IP 차단)면 즉시 중단 — 우회하지 않는다.
_BYDATE_INVESTORS = {"외국인": "forgn", "기타외국인": "forgn", "기관합계": "inst", "개인": "indiv"}


def _bt_cache_range(btd) -> tuple[str, str] | None:
    """캐시 파일명 {code}_{start}_{end}.pkl 에서 가장 흔한 구간."""
    from collections import Counter  # noqa: WPS433
    cnt: Counter = Counter()
    for kind in ("flow", "fund"):
        d = btd.CACHE_DIR / kind
        if d.exists():
            for p in d.glob("*_????????_????????.pkl"):
                cnt[tuple(p.stem.split("_")[1:3])] += 1
    if not cnt:
        return None
    return cnt.most_common(1)[0][0]


def _nz(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _from_bt_cache(btd, codes: list[str], start: str, end: str) -> dict:
    """캐시 구간 ∩ [start,end] 를 파일에서 읽어 flow/fund 에 넣는다. 네트워크 없음."""
    rng = _bt_cache_range(btd)
    if rng is None:
        return {"range": None, "hit": 0, "miss": len(codes), "rows_flow": 0, "rows_fund": 0}
    cs, ce = rng
    lo, hi = max(start, cs), min(end, ce)
    r = {"range": (cs, ce), "hit": 0, "miss": 0, "rows_flow": 0, "rows_fund": 0}
    if lo > hi:
        return r
    lo_ts, hi_ts = pd.Timestamp(lo), pd.Timestamp(hi)
    for code in codes:
        fl = btd._load(btd.CACHE_DIR / "flow" / f"{code}_{cs}_{ce}.pkl")
        fu = btd._load(btd.CACHE_DIR / "fund" / f"{code}_{cs}_{ce}.pkl")
        fl = None if isinstance(fl, str) else fl      # 옛 "ERR:" 항목은 없는 것으로
        fu = None if isinstance(fu, str) else fu
        if fl is None and fu is None:
            r["miss"] += 1
            continue
        r["hit"] += 1
        if fl is not None and not fl.empty:
            fl = fl.loc[(fl.index >= lo_ts) & (fl.index <= hi_ts)]
            r["rows_flow"] += store.upsert_flow(
                [{"code": code, "date": ix.strftime("%Y%m%d"),
                  "forgn": float(x.get("forgn", 0) or 0), "inst": float(x.get("inst", 0) or 0),
                  "indiv": float(x.get("indiv", 0) or 0)} for ix, x in fl.iterrows()])
        if fu is not None and not fu.empty:
            fu = fu.loc[(fu.index >= lo_ts) & (fu.index <= hi_ts)]
            r["rows_fund"] += store.upsert_fund(
                [{"code": code, "date": ix.strftime("%Y%m%d"),
                  "per": _nz(x.get("PER")), "pbr": _nz(x.get("PBR")), "eps": _nz(x.get("EPS"))}
                 for ix, x in fu.iterrows()])
    return r


def _bydate(btd, kind: str, d: str, fn, *args):
    """날짜 단위 캐시 + 호출. 실패는 캐시에 남기지 않는다(다음 실행에 다시 시도)."""
    p = btd.CACHE_DIR / "bydate" / f"{d}_{kind}.pkl"
    hit = btd._load(p)
    if isinstance(hit, pd.DataFrame):
        return hit, True
    df = btd._retry(fn, *args)          # Blocked 면 그대로 위로
    if df is None:
        df = pd.DataFrame()
    btd._save(p, df)
    return df, False


def _coverage(table: str, dates: list[str], n_codes: int) -> dict[str, float]:
    if not dates:
        return {}
    rows = store.conn().execute(
        f"SELECT date, COUNT(*) FROM {table} WHERE date BETWEEN ? AND ? GROUP BY date",  # noqa: S608
        (dates[0], dates[-1])).fetchall()
    return {r[0]: r[1] / max(n_codes, 1) for r in rows}


def load_history_pykrx(codes: list[str], start: str, end: str, rps: float = 0.7,
                       verbose: bool = True, min_cov: float = 0.9) -> dict:
    """수급·PER/PBR 과거분을 flow/fund 에 넣는다. 캐시 구간은 파일, 나머지는 날짜 루프.

    이미 flow(fund) 가 종목의 min_cov 이상 들어 있는 날짜는 건너뛴다(KIS 30일치 등).
    반환 {"cache_range","cache_hit","cache_miss","dates_total","dates_net","dates_cached",
          "dates_skipped","calls","fail_dates","rows_flow","rows_fund","sec"}.
    """
    from bt_swing import data as btd  # noqa: WPS433
    from dotenv import load_dotenv  # noqa: WPS433
    load_dotenv(ROOT / ".env")
    if rps > 0.7:
        raise ValueError("pykrx rps 는 0.7 이하")
    btd.set_rps(rps)
    t0 = time.time()
    code_set = set(codes)

    r0 = _from_bt_cache(btd, codes, start, end)
    logger.info("pykrx 캐시 구간 {}: 적중 {}종목 / 미스 {}종목, flow {}행 fund {}행",
                r0["range"], r0["hit"], r0["miss"], r0["rows_flow"], r0["rows_fund"])
    out = {"cache_range": r0["range"], "cache_hit": r0["hit"], "cache_miss": r0["miss"],
           "rows_flow": r0["rows_flow"], "rows_fund": r0["rows_fund"],
           "dates_total": 0, "dates_net": 0, "dates_cached": 0, "dates_skipped": 0,
           "calls": 0, "fail_dates": [], "sec": 0.0}

    # 캐시 밖 영업일
    net_start = start
    if r0["range"] and r0["range"][1] >= start:
        net_start = (datetime.strptime(r0["range"][1], "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    if net_start > end:
        out["sec"] = time.time() - t0
        return out
    dates = store.trading_dates(net_start, end, IDX_CODE)
    if not dates:
        try:
            dates = btd.trading_days(net_start, end)
        except btd.Blocked:
            raise Blocked("KRX 차단 — 영업일 조회") from None
    out["dates_total"] = len(dates)
    cov_flow = _coverage("flow", dates, len(codes))
    cov_fund = _coverage("fund", dates, len(codes))
    stock = btd._lazy_stock()

    for i, d in enumerate(dates, 1):
        need_flow = cov_flow.get(d, 0.0) < min_cov
        need_fund = cov_fund.get(d, 0.0) < min_cov
        if not need_flow and not need_fund:
            out["dates_skipped"] += 1
            continue
        try:
            cached_all = True
            if need_fund:
                fu, c = _bydate(btd, "fund", d, stock.get_market_fundamental, d, "ALL")
                cached_all &= c
                out["calls"] += 0 if c else 1
                if not fu.empty:
                    fu = fu.rename(columns=str.upper)
                    out["rows_fund"] += store.upsert_fund(
                        [{"code": tk, "date": d, "per": _nz(x.get("PER")), "pbr": _nz(x.get("PBR")),
                          "eps": _nz(x.get("EPS"))} for tk, x in fu.iterrows() if tk in code_set])
            if need_flow:
                acc: dict[str, dict[str, float]] = {}
                for inv, col in _BYDATE_INVESTORS.items():
                    n, c = _bydate(btd, f"net_{inv}", d,
                                   stock.get_market_net_purchases_of_equities, d, d, "ALL", inv)
                    cached_all &= c
                    out["calls"] += 0 if c else 1
                    if n.empty or "순매수거래대금" not in n.columns:
                        continue
                    for tk, v in n["순매수거래대금"].items():
                        if tk in code_set:
                            acc.setdefault(tk, {"forgn": 0.0, "inst": 0.0, "indiv": 0.0})[col] += float(v or 0)
                if acc:
                    out["rows_flow"] += store.upsert_flow(
                        [{"code": tk, "date": d, **v} for tk, v in acc.items()])
            out["dates_cached" if cached_all else "dates_net"] += 1
        except btd.Blocked as e:
            raise Blocked(f"KRX 차단 — pykrx 날짜 루프 {d} 에서 중단: {e}") from None
        except Exception as e:  # noqa: BLE001  (차단 아닌 실패: 캐시 안 남기고 다음 날짜)
            out["fail_dates"].append(d)
            logger.warning("pykrx {} 실패({}): {}", d, type(e).__name__, str(e)[:100])
        if verbose and (i % 20 == 0 or i == len(dates)):
            logger.info("pykrx 날짜 {}/{} 신규 {} 캐시 {} 건너뜀 {} 실패 {} 콜 {} {:.0f}s",
                        i, len(dates), out["dates_net"], out["dates_cached"], out["dates_skipped"],
                        len(out["fail_dates"]), out["calls"], time.time() - t0)
    out["sec"] = time.time() - t0
    return out


# ── 메인 수집 ─────────────────────────────────────────────────────────
def collect_daily(codes: list[str], count: int = 300, rps: float = 1.0,
                  budget_sec: float = 0, *, with_program: bool = True,
                  with_investor: bool = True, full: bool = False,
                  broker: DataBroker | None = None,
                  progress: Callable[[int, int], None] | None = None) -> dict:
    """codes 의 일봉·수급(30일)·밸류(당일)·프로그램 을 KIS 로 받아 DB 에 넣는다.

    증분: daily 의 마지막 날짜 다음날부터(full=False). 없으면 count 봉.
    budget_sec > 0 이면 그 시간 안에 못 끝낸 종목은 다음 실행으로 넘긴다(끝낸 만큼 기록).
    반환 {"ok","fail","skipped","failed":[...]}; 실패율 20% 초과면 Blocked.
    """
    b = broker or DataBroker(rps=rps)
    own = broker is None
    today = datetime.now().strftime("%Y%m%d")
    last = {} if full else store.last_dates("daily")
    n_ok, n_fail = 0, 0
    failed: list[str] = []
    skipped = 0
    t0 = time.time()
    try:
        for i, code in enumerate(codes, 1):
            if budget_sec and time.time() - t0 > budget_sec:
                skipped = len(codes) - i + 1
                logger.warning("collect_daily: 시간 예산 초과 — {}종목 미수집", skipped)
                break
            ld = last.get(code)
            if ld and ld >= today:
                n_ok += 1
                continue
            start = ((datetime.strptime(ld, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
                     if ld else (datetime.now() - timedelta(days=int(count * 1.6) + 10)).strftime("%Y%m%d"))
            try:
                rows, meta = fetch_daily(b, code, start, today)
                store.upsert_daily(rows)
                if meta:
                    store.upsert_meta([{"code": code, "name": meta.get("name") or None,
                                        "mktcap": int(meta["mktcap"]) if meta.get("mktcap") else None,
                                        "shares": int(meta["shares"]) if meta.get("shares") else None}])
                    if rows and meta.get("per") is not None:
                        # output1 밸류는 조회 시점(당일) 값 → 마지막 봉 날짜에 붙인다
                        store.upsert_fund([{"code": code, "date": rows[-1]["date"],
                                            "per": meta["per"], "pbr": meta["pbr"],
                                            "eps": meta["eps"]}])
                if with_investor:
                    store.upsert_flow(fetch_investor(b, code))
                if with_program:
                    pstart = start if ld else (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
                    store.upsert_program(fetch_program(b, code, pstart, today))
                n_ok += 1
            except (httpx.HTTPStatusError, RuntimeError, ValueError, KeyError) as e:
                n_fail += 1
                failed.append(code)
                logger.warning("collect_daily {} 실패: {}", code, str(e)[:160])
            done = n_ok + n_fail
            if progress:
                progress(done, len(codes))
            elif done % 100 == 0:
                logger.info("collect_daily {}/{} ok={} fail={} {:.0f}s", done, len(codes),
                            n_ok, n_fail, time.time() - t0)
            if done >= 50 and n_fail / done > _MAX_FAIL_RATIO:
                raise Blocked(f"KIS 수집 실패율 {n_fail}/{done} — 중단")
    finally:
        if own:
            b.close()
    return {"ok": n_ok, "fail": n_fail, "skipped": skipped, "failed": failed}


def collect_index(count: int = 400, broker: DataBroker | None = None) -> int:
    b = broker or DataBroker()
    try:
        today = datetime.now().strftime("%Y%m%d")
        ld = store.last_date("daily", IDX_CODE)
        start = ((datetime.strptime(ld, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
                 if ld else (datetime.now() - timedelta(days=int(count * 1.6) + 10)).strftime("%Y%m%d"))
        if ld and ld >= today:
            return 0
        rows = fetch_index(b, start, today)
        store.upsert_meta([{"code": IDX_CODE, "name": "KOSPI", "market": "INDEX"}])
        return store.upsert_daily(rows)
    finally:
        if broker is None:
            b.close()


def refresh_universe() -> int:
    """pykrx 로 전종목 코드·시장 을 meta 에 반영. 반환: 종목 수."""
    u = universe()
    store.upsert_meta(u)
    return len(u)


def check_today_complete(date: str, min_ratio: float = 0.9) -> tuple[bool, str]:
    """오늘자 일봉이 유니버스의 min_ratio 이상 들어왔는지 (명세 11절: 없으면 스캔 안 함)."""
    codes = store.all_codes()
    if not codes:
        return False, "universe empty"
    n = store.conn().execute("SELECT COUNT(*) FROM daily WHERE date=? AND code NOT LIKE 'IDX%'",
                             (date,)).fetchone()[0]
    ratio = n / len(codes)
    return ratio >= min_ratio, f"{n}/{len(codes)} ({ratio:.0%})"
