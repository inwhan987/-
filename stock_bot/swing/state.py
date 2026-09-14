# -*- coding: utf-8 -*-
"""상태 머신 (명세 9절).

WATCH → ARMED → ENTERED → HOLDING → EXIT
  └────────┴──────────┴──────→ DROPPED(사유)

WATCH 는 watchlist 행(DB positions 에 없음). 트리거가 나면 ARMED 로 positions 에
생기고, 주문 → ENTERED(체결 확인 전) → HOLDING(체결 확인) → EXIT.
DROPPED 사유는 반드시 기록한다: 셋업 붕괴 / 레짐 악화 / 미체결 반복 / 슬롯 없음.
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger

from . import store

WATCH, ARMED, ENTERED, HOLDING, EXIT, DROPPED = "WATCH", "ARMED", "ENTERED", "HOLDING", "EXIT", "DROPPED"

DROP_SETUP = "셋업 붕괴"
DROP_REGIME = "레짐 악화"
DROP_NOFILL = "미체결 반복"
DROP_NOSLOT = "슬롯 없음"

_ALLOWED = {
    ARMED: {ENTERED, DROPPED},
    ENTERED: {HOLDING, DROPPED},
    HOLDING: {EXIT},
}


def _now_hms() -> str:
    return datetime.now().strftime("%H%M%S")


def arm(mode: str, code: str, strategy: str, signal_date: str, trade_date: str,
        stop_px: float, tp_px: float, note: str = "") -> dict:
    pos = {
        "mode": mode, "code": code, "strategy": strategy, "state": ARMED,
        "entry_date": trade_date, "entry_time": _now_hms(), "entry_px": None, "shares": 0,
        "stop_px": stop_px, "tp_px": tp_px, "peak": None, "trail_on": 0,
        "signal_date": signal_date, "note": note,
    }
    pos["id"] = store.upsert_position(pos)
    logger.info("[{}] ARMED {} {} {}", mode, code, strategy, note)
    return pos


def transition(pos: dict, new: str, **fields) -> dict:
    cur = pos.get("state")
    if new not in _ALLOWED.get(cur, set()):
        raise ValueError(f"상태 전이 불가 {cur} → {new} ({pos.get('code')})")
    pos.update(fields)
    pos["state"] = new
    store.upsert_position(pos)
    logger.info("[{}] {} → {} {} {}", pos.get("mode"), cur, new, pos.get("code"),
                {k: v for k, v in fields.items()
                 if k in ("entry_px", "shares", "exit_reason", "exit_px", "note")})
    return pos


def entered(pos: dict, order_no: str | None, shares: int, entry_px: float) -> dict:
    return transition(pos, ENTERED, order_no=order_no, shares=shares, entry_px=entry_px,
                      entry_time=_now_hms())


def holding(pos: dict, shares: int, entry_px: float, stop_px: float, tp_px: float) -> dict:
    return transition(pos, HOLDING, shares=shares, entry_px=entry_px, stop_px=stop_px,
                      tp_px=tp_px, peak=entry_px, trail_on=0)


def drop(pos: dict, reason: str, detail: str = "") -> dict:
    note = reason + (f": {detail}" if detail else "")
    return transition(pos, DROPPED, exit_reason=reason, exit_date=pos.get("entry_date"),
                      exit_time=_now_hms(), note=(pos.get("note") or "") + " | " + note)


def exit_(pos: dict, reason: str, exit_px: float, date: str, order_no: str | None = None) -> dict:
    fields = dict(exit_reason=reason, exit_px=exit_px, exit_date=date, exit_time=_now_hms())
    if order_no:
        fields["note"] = (pos.get("note") or "") + f" | exit_odno={order_no}"
    return transition(pos, EXIT, **fields)
