# -*- coding: utf-8 -*-
"""청산 판정 (명세 8절). 반환 (True, 사유, 청산가) 또는 None.

  check_tick : 손절·익절 — 틱 즉시
  check_bar  : 트레일링 — 3분봉 확정 시 (peak 갱신 포함)
  check_eod  : 타임스톱·추세이탈 — 15:20

pos 는 store.positions 행(dict). peak/trough/trail_on 은 여기서 갱신해 돌려주므로
호출측은 판정 뒤 upsert_position 을 해야 한다. 레짐은 청산에 안 걸린다.
bt_swing.engine 의 순서(손절 → 익절 → 트레일링 → 타임스톱 → 이평선 이탈)를 그대로 따른다.

규칙은 전략별 ExitRule(cfg.exit_rule(strategy)). tp_px 가 None 이면 익절 없음, trail_pct<=0 이면
트레일링 없음, time_stop_days<=0 이면 타임스톱 없음, ma_exit=0 이면 이평선 청산 없음.
peak/trough 는 청산 후 MFE/MAE 로 남겨 전략별 손절·익절 재조정의 실측 근거가 된다.
"""
from __future__ import annotations

from .config import ExitRule

ExitResult = tuple[bool, str, float] | None


def _tp(pos: dict) -> float | None:
    v = pos.get("tp_px")
    return float(v) if v else None


def check_tick(pos: dict, price: float, r: ExitRule) -> ExitResult:
    if price <= float(pos["stop_px"]):
        return True, "손절", price
    tp = _tp(pos)
    if tp is not None and price >= tp:
        return True, "익절", price
    return None


def check_bar(pos: dict, bar, r: ExitRule) -> ExitResult:
    """bar: kis_ws.Bar. 손절·익절도 봉 고저로 한 번 더 본다(틱 누락 대비)."""
    entry = float(pos["entry_px"])
    # MFE/MAE 추적 — 청산 판정과 무관하게 항상 갱신
    pos["peak"] = max(float(pos.get("peak") or entry), float(bar.high))
    pos["trough"] = min(float(pos.get("trough") or entry), float(bar.low))
    if float(bar.low) <= float(pos["stop_px"]):
        return True, "손절", float(pos["stop_px"])
    tp = _tp(pos)
    if tp is not None and float(bar.high) >= tp:
        return True, "익절", tp
    if r.trail_pct <= 0:
        return None
    peak = float(pos["peak"])
    if not pos.get("trail_on") and float(bar.high) >= entry * (1 + r.trail_after):
        pos["trail_on"] = 1
    if pos.get("trail_on") and float(bar.close) <= peak * (1 - r.trail_pct):
        return True, "트레일링", float(bar.close)
    return None


def check_eod(pos: dict, day_close: float, r: ExitRule, *,
              held_days: int, ma: float | None = None) -> ExitResult:
    """held_days: 진입일 포함 보유 거래일 수. ma: 당일 기준 r.ma_exit 일선(이평선 이탈용)."""
    if r.time_stop_days > 0 and held_days >= r.time_stop_days:
        return True, "타임스톱", day_close
    if r.ma_exit > 0 and ma is not None and day_close < ma:
        return True, f"{r.ma_exit}일선이탈", day_close
    return None
