# -*- coding: utf-8 -*-
"""청산 판정 (명세 8절). 반환 (True, 사유, 청산가) 또는 None.

  check_tick : 손절·익절 — 틱 즉시
  check_bar  : 트레일링 — 3분봉 확정 시 (peak 갱신 포함)
  check_eod  : 타임스톱·추세이탈 — 15:20

pos 는 store.positions 행(dict). peak/trail_on 은 여기서 갱신해 돌려주므로
호출측은 판정 뒤 upsert_position 을 해야 한다. 레짐은 청산에 안 걸린다.
bt_swing.engine 의 순서(손절 → 익절 → 트레일링 → 타임스톱)를 그대로 따른다.
"""
from __future__ import annotations

from .config import SwingCfg

ExitResult = tuple[bool, str, float] | None


def check_tick(pos: dict, price: float, c: SwingCfg) -> ExitResult:
    if price <= float(pos["stop_px"]):
        return True, "손절", price
    if price >= float(pos["tp_px"]):
        return True, "익절", price
    return None


def check_bar(pos: dict, bar, c: SwingCfg) -> ExitResult:
    """bar: kis_ws.Bar. 손절·익절도 봉 고저로 한 번 더 본다(틱 누락 대비)."""
    if float(bar.low) <= float(pos["stop_px"]):
        return True, "손절", float(pos["stop_px"])
    if float(bar.high) >= float(pos["tp_px"]):
        return True, "익절", float(pos["tp_px"])
    entry = float(pos["entry_px"])
    peak = max(float(pos.get("peak") or entry), float(bar.high))
    pos["peak"] = peak
    if not pos.get("trail_on") and float(bar.high) >= entry * (1 + c.trail_after):
        pos["trail_on"] = 1
    if pos.get("trail_on") and float(bar.close) <= peak * (1 - c.trail_pct):
        return True, "트레일링", float(bar.close)
    return None


def check_eod(pos: dict, day_close: float, c: SwingCfg, *,
              held_days: int, ma20: float | None = None) -> ExitResult:
    """held_days: 진입일 포함 보유 거래일 수. ma20: 당일 기준 20일선(추세이탈용)."""
    if held_days >= c.time_stop_days:
        return True, "타임스톱", day_close
    if c.exit_trend_break and ma20 is not None and day_close < ma20:
        return True, "추세이탈", day_close
    return None
