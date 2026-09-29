"""dart_hist.jsonl → 임의 as-of 날짜의 재무 재료(roe/debt/qrev/qinc) 오프라인 재구성.

dart.fetch_financials() 와 같은 선택 규칙(_qtr_candidates 순서, 연간은 작년→재작년)을
쓰되, 접수일(rcept_dt) 이 as-of 날짜보다 늦은 보고서는 건너뛴다. 실전에서는 미공시
보고서가 API 에서 013 으로 빠지기 때문에 자연히 룩어헤드가 없지만, 과거 시점을
재구성할 때는 접수일로 직접 걸러야 한다 — 이 파일의 존재 이유가 그것이다.

bt 쪽 축(report.py:250)은 roe/debt/qrev/qinc 를 늘 NaN 으로 두고 있었다. 이 모듈이
그 자리를 채운다.

    python scripts/research/dart_asof.py            # 자가검증(현재 캐시와 대조)
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HIST = ROOT / "data" / "research" / "dart_hist.jsonl"

RTYPE_ANNUAL = "11011"


def _qtr_candidates(year: int, month: int) -> list[tuple[int, str]]:
    """dart._qtr_candidates 와 동일 — 달력 기준 후보 순서."""
    y, m = year, month
    if m >= 11:
        return [(y, "11014"), (y, "11012"), (y - 1, "11014")]
    if m >= 8:
        return [(y, "11012"), (y, "11014"), (y - 1, "11014")]
    if m >= 5:
        return [(y, "11013"), (y - 1, "11014"), (y - 1, "11012")]
    return [(y - 1, "11014"), (y - 1, "11012"), (y - 1, "11013")]


def load() -> dict[str, dict[tuple[int, str], dict]]:
    """JSONL → {code: {(year, rtype): rec}}. nodata 행은 버린다."""
    out: dict[str, dict[tuple[int, str], dict]] = defaultdict(dict)
    with HIST.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("nodata") or not r.get("rcept_dt"):
                continue
            out[r["code"]][(int(r["year"]), r["rtype"])] = r
    return dict(out)


def materials(byc: dict[tuple[int, str], dict], asof: str) -> dict[str, float]:
    """asof = 'YYYYMMDD'. 반환 키는 axes.MATERIALS 이름(roe/debt/qrev/qinc). 없으면 빠짐."""
    out: dict[str, float] = {}
    y, m = int(asof[:4]), int(asof[4:6])

    def filed(key: tuple[int, str]) -> dict | None:
        r = byc.get(key)
        return r if r and r["rcept_dt"] <= asof else None

    for ay in (y - 1, y - 2):
        fs = filed((ay, RTYPE_ANNUAL))
        if not fs:
            continue
        inc, eq, debt = fs["inc_th"], fs["equity"], fs["debt"]
        if inc and eq:
            out["roe"] = inc / abs(eq)
        if debt is not None and eq:
            out["debt"] = debt / abs(eq) * 100
        break

    for qy, qt in _qtr_candidates(y, m):
        cur = filed((qy, qt))
        if not cur:
            continue
        prv = filed((qy - 1, qt))
        if prv:
            a, b = cur["rev_th"], prv["rev_th"]
            if a and b:
                out["qrev"] = (a - b) / abs(b)
            a, b = cur["inc_th"], prv["inc_th"]
            if a and b:
                out["qinc"] = (a - b) / abs(b)
        break
    return out


def _selfcheck() -> None:
    """현재 시점 재구성 vs data/swing_dart_cache.json(실전이 실제로 쓰는 값) 대조.
    캐시는 살아있는 코드가 DART 에서 직접 받아 계산한 값이므로, 일치하면 재구성 규칙이
    실전과 같다는 뜻이다. 접수일 필터 때문에 캐시보다 한 분기 이전을 고르는 경우는
    불일치가 아니라 '캐시가 더 최신'이라 정상 — 그 건수도 따로 센다."""
    import datetime as dt

    hist = load()
    cache = json.loads((ROOT / "data" / "swing_dart_cache.json").read_text(encoding="utf-8"))
    rows = cache.get("data") if isinstance(cache, dict) and "data" in cache else cache
    asof = dt.date.today().strftime("%Y%m%d")
    MAP = {"roe": "returnOnEquity", "debt": "debtToEquity",
           "qrev": "qtr_rev_growth", "qinc": "qtr_inc_growth"}
    n = same = diff = miss = 0
    worst = []
    for code, byc in hist.items():
        ent = rows.get(code)
        if not isinstance(ent, dict):
            continue
        live = ent.get("data", ent)
        got = materials(byc, asof)
        for k, lk in MAP.items():
            lv, gv = live.get(lk), got.get(k)
            if lv is None:
                continue
            n += 1
            if gv is None:
                miss += 1
                continue
            rel = abs(gv - lv) / max(abs(lv), 1e-9)
            if rel < 1e-6:
                same += 1
            else:
                diff += 1
                worst.append((rel, code, k, lv, gv))
    print(f"대조 {n}건 / 일치 {same} / 불일치 {diff} / 재구성에없음 {miss}")
    for rel, code, k, lv, gv in sorted(worst, reverse=True)[:10]:
        print(f"  {code} {k}: 실전 {lv:.6g} vs 재구성 {gv:.6g} (상대차 {rel:.3g})")


if __name__ == "__main__":
    _selfcheck()
