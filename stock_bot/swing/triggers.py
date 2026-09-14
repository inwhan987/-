# -*- coding: utf-8 -*-
"""장중 트리거 (명세 7절). 전략 유형별로 분리 — 섞지 않는다.

check(strategy, bar, lv, session) -> (bool, reason)
  bar     : kis_ws.Bar (3분봉 확정)
  lv      : watchlist 행 (ref_* 기준선)
  session : {"session_low": float, "session_high": float, "bars": int}
"""
from __future__ import annotations

from .daily_scan import TRIGGER_KIND


def _f(v) -> float | None:
    return None if v is None else float(v)


def breakout_trigger(bar, lv, s) -> tuple[bool, str]:
    top, avg = _f(lv.get("ref_box_top")), _f(lv.get("ref_avg_bar_vol"))
    if top is None or avg is None:
        return False, "기준선없음"
    if bar.close <= top:
        return False, "박스미돌파"
    if bar.volume <= avg * 1.5:
        return False, "거래량부족"
    return True, "돌파"


def pullback_trigger(bar, lv, s) -> tuple[bool, str]:
    ma20, pc = _f(lv.get("ref_ma20")), _f(lv.get("ref_prev_close"))
    if ma20 is None or pc is None:
        return False, "기준선없음"
    if bar.low > ma20 * 1.02:
        return False, "미눌림"
    if bar.close <= bar.open:
        return False, "음봉"
    if bar.close <= pc:
        return False, "전일종가하회"
    if bar.close <= s["session_low"] * 1.005:
        return False, "저점근접"
    return True, "눌림반등"


def hold_trigger(bar, lv, s) -> tuple[bool, str]:
    pc = _f(lv.get("ref_prev_close"))
    if pc is None:
        return False, "기준선없음"
    if bar.close <= pc:
        return False, "전일종가하회"
    if bar.close <= s["session_low"] * 1.005:
        return False, "저점근접"
    if bar.vwap and bar.close < bar.vwap:
        return False, "VWAP하회"
    return True, "유지"


_FN = {"breakout": breakout_trigger, "pullback": pullback_trigger, "hold": hold_trigger}


def kind(strategy: str) -> str:
    return TRIGGER_KIND.get(strategy.upper(), "hold")


def check(strategy: str, bar, lv: dict, session: dict) -> tuple[bool, str]:
    return _FN[kind(strategy)](bar, lv, session)
