"""대장주 선별 시점 수급·체결강도 기록 (기록 전용 — 매매·선별 비간섭).

2026-10-03: 09:30 공개 지표(거래대금·등락·회전율·배수·이격·시장폭)로는 1위 종목의
성패를 더 가를 수 없다는 결론 → 아직 안 본 축을 앞으로 쌓아서 검증한다.

  1) 선별 직후 (snapshot): 1등 섹터 top3 에 대해
     - 체결강도      inquire-ccnl  FHKST01010300  tday_rltv
     - 호가 총잔량   get_orderbook (3회 찍어 중앙값 — 순간값이라 흔들린다)
     - 프로그램 순매수 program-trade-by-stock FHPPG04650101 (당일 누적)
     - 외국계 창구   inquire-member FHKST01010600 glob_* (당일 누적)
     위 넷은 최근 30틱/현재값만 주므로 장 끝나면 09:30 값을 되살릴 수 없다.
  2) 장 마감 후 (attach_estimate): 외인·기관 추정가집계 investor-trend-estimate
     HHPTJ04160200 — 마감 후에도 당일 5회차(1=09:30 … 5=14:30)가 전부 남는다.
     날짜 파라미터가 없어 과거일은 못 받는다(당일 15:40 에 받아 둔다).

결과는 data/leader_picks/<날짜>_flow.json (일별 백업으로 git 에 올라간다).
웹소켓은 스윙(kis_ws)이 앱키 하나로 쓰고 있어 REST 스냅샷만 쓴다. 호출은
broker 유량 게이트를 거치고 호출 사이 1초를 더 쉰다(09:30 스윙 진입과 한도 공유).
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

_KST = ZoneInfo("Asia/Seoul")
_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "data" / "leader_picks"
_Q = "/uapi/domestic-stock/v1/quotations/"
_GAP = 1.0


def _flow_path(date: str) -> Path:
    return _DIR / f"{date}_flow.json"


def _num(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _get(broker, path: str, tr: str, params: dict, label: str) -> dict:
    try:
        j = broker._get_with_retry(_Q + path, tr, params, label=label).json()
    except Exception as exc:
        logger.debug("flow_probe {} 실패: {}", label, exc)
        return {}
    finally:
        time.sleep(_GAP)
    if str(j.get("rt_cd")) != "0":
        logger.debug("flow_probe {} rt_cd={} {}", label, j.get("rt_cd"), j.get("msg1"))
        return {}
    return j


def _first(j: dict, key: str = "output") -> dict:
    v = j.get(key)
    if isinstance(v, list):
        return v[0] if v else {}
    return v or {}


def _probe_one(broker, code: str) -> dict:
    p = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    rec: dict[str, Any] = {"at": datetime.now(tz=_KST).strftime("%H:%M:%S")}

    r = _first(_get(broker, "inquire-ccnl", "FHKST01010300", p, f"ccnl {code}"))
    rec["cttr"] = _num(r.get("tday_rltv"))
    rec["cttr_hour"] = r.get("stck_cntg_hour")

    books = []
    for i in range(3):
        try:
            ob = broker.get_orderbook(code) or {}
        except Exception:
            ob = {}
        a, b = ob.get("total_ask_qty"), ob.get("total_bid_qty")
        if a and b:
            books.append((int(a), int(b)))
        if i < 2:
            time.sleep(2.0)
    rec["book"] = books
    rec["bid_ask_ratio"] = (round(statistics.median(b / a for a, b in books), 3)
                            if books else None)

    r = _first(_get(broker, "program-trade-by-stock", "FHPPG04650101", p, f"program {code}"))
    rec["prog_ntby_qty"] = _num(r.get("whol_smtn_ntby_qty"))
    rec["prog_ntby_won"] = _num(r.get("whol_smtn_ntby_tr_pbmn"))
    rec["prog_hour"] = r.get("bsop_hour")

    r = _first(_get(broker, "inquire-member", "FHKST01010600", p, f"member {code}"))
    rec["glob_ntby_qty"] = _num(r.get("glob_ntby_qty"))
    rec["glob_buy_qty"] = _num(r.get("glob_total_shnu_qty"))
    rec["glob_sell_qty"] = _num(r.get("glob_total_seln_qty"))
    return rec


def snapshot(broker, date: str) -> None:
    """선별 직후 1등 섹터 top3 의 체결강도·호가·프로그램·외국계 창구를 기록."""
    out = _flow_path(date)
    if out.exists():
        return
    picks = _DIR / f"{date}.json"
    try:
        d = json.loads(picks.read_text(encoding="utf-8"))
        lead = (d.get("leaders") or [])[0]
    except Exception as exc:
        logger.warning("flow_probe: picks 읽기 실패 ({}): {}", picks.name, exc)
        return
    t0 = time.time()
    stocks = []
    for s in lead.get("top3") or []:
        code = str(s.get("code") or "")
        if not code:
            continue
        rec = {"code": code, "name": s.get("name"), "rank": s.get("rank"),
               "price": s.get("price"), "value_won": s.get("value_won"),
               "change_pct": s.get("change_pct")}
        rec.update(_probe_one(broker, code))
        stocks.append(rec)
    doc = {"date": date, "selected_at": d.get("selected_at"),
           "sector": lead.get("sector"), "stocks": stocks}
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(
        "flow_probe: {} 1등 섹터 {} {}종목 기록 ({:.0f}초) — {}", date, lead.get("sector"),
        len(stocks), time.time() - t0,
        " · ".join(f"{x['name']} 체결강도 {x['cttr']} 잔량비 {x['bid_ask_ratio']}"
                   f" 프로그램 {x['prog_ntby_won']} 외국계 {x['glob_ntby_qty']}" for x in stocks),
    )


def attach_estimate(broker, date: str) -> None:
    """장 마감 후 외인·기관 추정가집계 5회차를 붙인다 (당일에만 유효)."""
    out = _flow_path(date)
    if not out.exists():
        return
    try:
        doc = json.loads(out.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("flow_probe: {} 읽기 실패: {}", out.name, exc)
        return
    n = 0
    for s in doc.get("stocks") or []:
        j = _get(broker, "investor-trend-estimate", "HHPTJ04160200",
                 {"MKSC_SHRN_ISCD": s["code"]}, f"estimate {s['code']}")
        est = {}
        for row in j.get("output2") or []:
            gb = str(row.get("bsop_hour_gb") or "")
            if gb:
                est[gb] = {k: _num(row.get(f"{k}_fake_ntby_qty"))
                           for k in ("frgn", "orgn", "sum")}
        s["estimate"] = est   # 1=09:30 2=10:00 3=11:20 4=13:20 5=14:30 (주, 1000단위)
        n += bool(est)
    doc["estimate_at"] = datetime.now(tz=_KST).strftime("%H:%M:%S")
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("flow_probe: {} 추정가집계 {}/{}종목", date, n, len(doc.get("stocks") or []))
