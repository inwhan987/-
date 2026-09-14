# -*- coding: utf-8 -*-
"""DART 재무 지표 수집 (기록용 — 전략 조건/게이트에 쓰지 않는다).

명세 15절에 'DART 재무 등급 미구현' 으로 남겨둔 항목. 주간 배치(scripts/swing_dart_weekly.py)
가 전 종목의 연간 + 최근 분기 재무를 받아 dart_fin 테이블에 적어 둔다.
나중에 백테스트에서 팩터로 측정하기 위한 적재이지, 진입/청산 판단에는 안 들어간다.

점수에 붙일 때 쓰는 날짜는 rcept_dt(공시 접수일 = rcept_no 앞 8자리). 연간·분기 보고서의
접수일이 다르므로 rcept_dt = 둘 중 늦은 날(보수적; 미래참조 없음), 각각은
rcept_dt_annual / rcept_dt_qtr 에 따로 둔다. fiscal 은 "2025A/2026H1" 꼴.

screener.py 의 _dart_financials 와 같은 계정명·같은 산식을 쓴다(그 파일은 수정 금지라
로직만 옮겼다). OpenDartReader 대신 requests 직접 호출.

  revenueGrowth  = (매출 당기 - 전기) / |전기|          연간
  earningsGrowth = (순이익 당기 - 전기) / |전기|        연간
  returnOnEquity = 순이익 / |자본총계|                  연간
  debtToEquity   = 부채총계 / |자본총계| * 100          연간
  qtr_rev_growth / qtr_inc_growth = 최근 분기 YoY

키는 환경변수 DART_API_KEY 만 읽고 절대 출력/저장하지 않는다.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from . import store
from .config import ROOT

_BASE = "https://opendart.fss.or.kr/api"
_TIMEOUT = 20
_SLEEP = 0.5                    # 호출 간격 (DART 속도제한 방지)
_CACHE_TTL_DAYS = 30
_CORP_TTL_DAYS = 7

CACHE_PATH = ROOT / "data" / "swing_dart_cache.json"
CORP_PATH = ROOT / "data" / "swing_dart_corp.json"

RTYPE_ANNUAL = "11011"
RTYPE_LABEL = {"11011": "연간", "11013": "Q1", "11012": "H1", "11014": "Q3"}

_REV_KEYS = ("매출액", "영업수익", "수익(매출액)")
_INC_KEYS = ("당기순이익", "당기순이익(손실)")

FIELDS = ["revenueGrowth", "earningsGrowth", "returnOnEquity", "debtToEquity",
          "qtr_rev_growth", "qtr_inc_growth", "qtr_label", "annual_year",
          "rcept_dt", "fiscal", "rcept_dt_annual", "rcept_dt_qtr"]


class DartError(RuntimeError):
    pass


def api_key() -> str:
    k = os.environ.get("DART_API_KEY", "").strip()
    if not k:
        raise DartError("DART_API_KEY 없음")
    return k


# ── 디스크 캐시 (screener 와 같은 방식: tmp 쓰고 replace) ──────────────
def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("DART 캐시 읽기 실패 {}: {}", path, e)
    return {}


def _save_json(path: Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("DART 캐시 쓰기 실패 {}: {}", path, e)


def _fresh(entry: dict, ttl_days: int) -> bool:
    try:
        ts = datetime.fromisoformat(entry.get("_ts", ""))
    except Exception:
        return False
    return datetime.now() - ts < timedelta(days=ttl_days)


# ── corp_code (종목코드 → DART 고유번호) ───────────────────────────────
def load_corp_map(force: bool = False) -> dict[str, str]:
    cached = _load_json(CORP_PATH)
    if not force and cached.get("map") and _fresh(cached, _CORP_TTL_DAYS):
        return cached["map"]
    r = requests.get(f"{_BASE}/corpCode.xml", params={"crtfc_key": api_key()}, timeout=60)
    r.raise_for_status()
    if not r.content[:2] == b"PK":
        raise DartError(f"corpCode 응답이 zip 이 아님 (status={r.status_code})")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(z.namelist()[0]).decode("utf-8", errors="ignore")
    m: dict[str, str] = {}
    for blk in re.finditer(r"<list>(.*?)</list>", xml, flags=re.S):
        s = blk.group(1)
        cc = re.search(r"<corp_code>(\d+)</corp_code>", s)
        sc = re.search(r"<stock_code>\s*([0-9A-Z]{6})\s*</stock_code>", s)
        if cc and sc:
            m[sc.group(1)] = cc.group(1)
    if not m:
        raise DartError("corpCode 파싱 결과 0건")
    _save_json(CORP_PATH, {"_ts": datetime.now().isoformat(), "map": m})
    logger.info("DART corp_code {}건 갱신", len(m))
    return m


# ── 재무제표 단건 ───────────────────────────────────────────────────────
def _finstate(corp_code: str, year: int, rtype: str) -> list[dict] | None:
    """fnlttSinglAcnt. 013(데이터 없음) → 1회 재시도 후 None. 그 외 오류는 예외."""
    params = {"crtfc_key": api_key(), "corp_code": corp_code,
              "bsns_year": str(year), "reprt_code": rtype}
    for attempt in range(2):
        r = requests.get(f"{_BASE}/fnlttSinglAcnt.json", params=params, timeout=_TIMEOUT)
        time.sleep(_SLEEP)
        if r.status_code != 200:
            raise DartError(f"HTTP {r.status_code}")
        j = r.json()
        st = str(j.get("status"))
        if st == "000":
            return list(j.get("list") or [])
        if st == "013":
            if attempt == 0:
                time.sleep(2.0)
                continue
            return None
        if st == "020":
            raise DartError("DART 일일 호출 한도 초과(020)")
        raise DartError(f"status {st}: {j.get('message')}")
    return None


def _pick_div(rows: list[dict] | None) -> list[dict] | None:
    if not rows:
        return None
    for div in ("CFS", "OFS"):
        sub = [x for x in rows if x.get("fs_div") == div]
        if sub:
            return sub
    return rows


def _amount(rows: list[dict], *keys: str, col: str = "thstrm_amount") -> float | None:
    for kw in keys:
        for x in rows:
            if kw in str(x.get("account_nm") or ""):
                try:
                    v = float(str(x.get(col) or "").replace(",", "").replace(" ", ""))
                except ValueError:
                    continue
                if v != 0:
                    return v
                break
    return None


def _rcept_dt(rows: list[dict] | None) -> str | None:
    """응답 행의 rcept_no 앞 8자리 = 접수일(YYYYMMDD). 실제 응답에 rcept_no 가 있는 것을
    확인하고 넣었다(2026-09-14, 삼성전자 2025 연간 20260310002820)."""
    if not rows:
        return None
    for x in rows:
        v = str(x.get("rcept_no") or "")
        if len(v) >= 8 and v[:8].isdigit():
            return v[:8]
    return None


def _qtr_candidates(now: datetime) -> list[tuple[int, str]]:
    y, m = now.year, now.month
    if m >= 11:
        return [(y, "11014"), (y, "11012"), (y - 1, "11014")]
    if m >= 8:
        return [(y, "11012"), (y, "11014"), (y - 1, "11014")]
    if m >= 5:
        return [(y, "11013"), (y - 1, "11014"), (y - 1, "11012")]
    return [(y - 1, "11014"), (y - 1, "11012"), (y - 1, "11013")]


def fetch_financials(code: str, corp_code: str, now: datetime | None = None) -> dict[str, Any]:
    """한 종목. 반환 dict 의 키는 FIELDS 부분집합 (없으면 빠짐)."""
    now = now or datetime.now()
    out: dict[str, Any] = {}

    fs = None
    for y in (now.year - 1, now.year - 2):
        fs = _pick_div(_finstate(corp_code, y, RTYPE_ANNUAL))
        if fs:
            out["annual_year"] = y
            break
    if fs:
        if _rcept_dt(fs):
            out["rcept_dt_annual"] = _rcept_dt(fs)
        rev_c = _amount(fs, *_REV_KEYS)
        rev_p = _amount(fs, *_REV_KEYS, col="frmtrm_amount")
        inc_c = _amount(fs, *_INC_KEYS)
        inc_p = _amount(fs, *_INC_KEYS, col="frmtrm_amount")
        eq = _amount(fs, "자본총계")
        debt = _amount(fs, "부채총계")
        if rev_c is None and inc_c is None:
            logger.debug("DART 계정명 불일치 {}: {}",
                         code, sorted({x.get("account_nm") for x in fs})[:10])
        if rev_c and rev_p:
            out["revenueGrowth"] = (rev_c - rev_p) / abs(rev_p)
        if inc_c and inc_p:
            out["earningsGrowth"] = (inc_c - inc_p) / abs(inc_p)
        if inc_c and eq:
            out["returnOnEquity"] = inc_c / abs(eq)
        if debt is not None and eq:
            out["debtToEquity"] = debt / abs(eq) * 100

    for qy, qt in _qtr_candidates(now):
        cur = _pick_div(_finstate(corp_code, qy, qt))
        if not cur:
            continue
        out["qtr_label"] = f"{qy} {RTYPE_LABEL[qt]}"
        if _rcept_dt(cur):
            out["rcept_dt_qtr"] = _rcept_dt(cur)
        prv = _pick_div(_finstate(corp_code, qy - 1, qt))
        if prv:
            a, b = _amount(cur, *_REV_KEYS), _amount(prv, *_REV_KEYS)
            if a and b:
                out["qtr_rev_growth"] = (a - b) / abs(b)
            a, b = _amount(cur, *_INC_KEYS), _amount(prv, *_INC_KEYS)
            if a and b:
                out["qtr_inc_growth"] = (a - b) / abs(b)
        break

    dts = [d for d in (out.get("rcept_dt_annual"), out.get("rcept_dt_qtr")) if d]
    if dts:
        out["rcept_dt"] = max(dts)          # 둘 다 받은 뒤라야 쓸 수 있는 값 → 늦은 접수일
    parts = []
    if "annual_year" in out:
        parts.append(f"{out['annual_year']}A")
    if "qtr_label" in out:
        parts.append(out["qtr_label"].replace(" ", ""))
    if parts:
        out["fiscal"] = "/".join(parts)
    return out


# ── 배치 ───────────────────────────────────────────────────────────────
def collect(codes: list[str], date: str, *, budget_sec: int = 0,
            corp_map: dict[str, str] | None = None, force: bool = False) -> dict[str, int]:
    """codes 의 재무를 받아 dart_fin 에 적재. 캐시(30일) 안이면 API 안 부른다(force=True 면 무시).
    반환 {"ok","cached","nodata","error","skipped"}. 키가 없으면 DartError.
    예산을 넘기면 나머지는 skipped 로 끊는다 — 캐시 덕에 다음 실행이 이어받는다.
    """
    key_ok = bool(os.environ.get("DART_API_KEY", "").strip())
    if not key_ok:
        raise DartError("DART_API_KEY 없음 — DART 적재 건너뜀")
    cache = _load_json(CACHE_PATH)
    corp_map = corp_map or load_corp_map()
    t0 = time.time()
    n = {"ok": 0, "cached": 0, "nodata": 0, "error": 0, "skipped": 0}
    rows: list[dict] = []
    dirty = False
    for i, code in enumerate(dict.fromkeys(codes)):
        if budget_sec and time.time() - t0 > budget_sec:
            n["skipped"] += len(codes) - i
            logger.warning("DART 예산 {}s 초과 — {}종목 미수집", budget_sec, n["skipped"])
            break
        ent = None if force else cache.get(code)
        if ent and _fresh(ent, _CACHE_TTL_DAYS) and ("rcept_dt" in ent or not ent.get("annual_year")):
            fin = {k: v for k, v in ent.items() if k in FIELDS}
            n["cached"] += 1
        else:
            cc = corp_map.get(code)
            if not cc:
                n["nodata"] += 1
                continue
            try:
                fin = fetch_financials(code, cc)
            except DartError as e:
                if "020" in str(e):
                    raise
                logger.warning("DART {} 실패: {}", code, e)
                n["error"] += 1
                continue
            except requests.RequestException as e:
                logger.warning("DART {} 네트워크 오류: {}", code, e)
                n["error"] += 1
                continue
            cache[code] = {**fin, "_ts": datetime.now().isoformat()}
            dirty = True
            n["ok" if fin else "nodata"] += 1
            if not fin:
                continue
        rows.append({"date": date, "code": code, **fin})
    if dirty:
        _save_json(CACHE_PATH, cache)
    if rows:
        store.upsert_dart_fin(rows)
    logger.info("DART 적재 {} — {}", date, n)
    return n
