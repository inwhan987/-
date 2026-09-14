# -*- coding: utf-8 -*-
"""signals / positions 의 fwd1·fwd3·fwd5 사후 채우기 (명세 13절 10, 2절 [사후]).

    fwdN = close(기준일 + N 거래일) / 기준가 - 1

  signals  : 기준일 = trade_date(감시일, 없으면 date 다음 거래일),
             기준가 = trigger_px(타점 났으면) / day_close(안 났으면 — 트리거 변별력 비교용).
             day_open/high/low/close 가 비어 있으면 daily 에서 같이 채운다.
  positions: 기준일 = entry_date, 기준가 = entry_px (HOLDING/EXIT 만).

일봉(daily)이 있는 만큼만 채우고, 아직 N 거래일이 안 지난 것은 NULL 로 둔다. 멱등.
    python scripts/swing_fill_fwd.py [--since YYYYMMDD]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from stock_bot.swing import store  # noqa: E402
from stock_bot.swing.collector import IDX_CODE  # noqa: E402
from stock_bot.swing.config import cfg  # noqa: E402

HORIZONS = (1, 3, 5)


def _closes(code: str, start: str) -> list[tuple[str, dict]]:
    q = ("SELECT date,open,high,low,close FROM daily WHERE code=? AND date>=? ORDER BY date")
    return [(r["date"], dict(r)) for r in store.conn().execute(q, (code, start))]


def _fwd(rows: list[tuple[str, dict]], base_date: str, base_px: float) -> dict:
    """rows 는 base_date 이후 일봉(오름차순). base_date 이후 N번째 거래일 종가."""
    after = [r for d, r in rows if d > base_date]
    out = {}
    for n in HORIZONS:
        if len(after) >= n and base_px:
            out[f"fwd{n}"] = after[n - 1]["close"] / base_px - 1
    return out


def fill_signals(since: str) -> int:
    cal = store.trading_dates(since, "99991231", IDX_CODE)
    rows = [dict(r) for r in store.conn().execute(
        "SELECT * FROM signals WHERE date>=? AND (fwd5 IS NULL OR day_close IS NULL)", (since,))]
    n = 0
    for s in rows:
        td = s.get("trade_date")
        if not td:
            nxt = [d for d in cal if d > s["date"]]
            if not nxt:
                continue
            td = nxt[0]
        daily = _closes(s["code"], td)
        if not daily or daily[0][0] != td:
            continue
        upd: dict = {"date": s["date"], "code": s["code"], "strategy": s["strategy"], "trade_date": td}
        day = daily[0][1]
        if s.get("day_close") is None:
            upd.update(day_open=day["open"], day_high=day["high"], day_low=day["low"],
                       day_close=day["close"])
        base = s.get("trigger_px") or day["close"]
        upd.update(_fwd(daily, td, float(base)))
        if len(upd) > 4:
            store.log_signal(upd)
            n += 1
    return n


def fill_positions(since: str) -> int:
    rows = [p for p in store.load_positions()
            if p.get("state") in ("HOLDING", "EXIT") and p.get("entry_px")
            and (p.get("entry_date") or "") >= since and p.get("fwd5") is None]
    n = 0
    for p in rows:
        daily = _closes(p["code"], p["entry_date"])
        f = _fwd(daily, p["entry_date"], float(p["entry_px"]))
        if f:
            p.update(f)
            store.upsert_position(p)
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="20000101")
    a = ap.parse_args()
    store.init_db(cfg().db_path)
    try:
        ns = fill_signals(a.since)
        np_ = fill_positions(a.since)
        logger.info("fill_fwd: signals {}건, positions {}건 갱신", ns, np_)
    finally:
        store.close()


if __name__ == "__main__":
    main()
