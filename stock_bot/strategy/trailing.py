"""Trailing Stop 4가지 모드 + 자동 선택.

종목별 trailing stop 로직. 매 틱마다 호출하여 청산 여부 결정.

Modes:
  A (Fixed):  수익 ≥1.5% 도달 → 손절선 = max(매수가, 최고가 × 0.995)
  B (ATR):    수익 ≥ ATR×3/가격 % → 손절선 = max(매수가, 최고가 - ATR×2.5)
  C (Step):   +1.5%/+3.0%/+5.0% 단계별 락인
  D (Hybrid): ATR 트레일 + 단계 락인 결합 (균형형)
  AUTO: ADX 기반 자동 선택 (>25 → A, 그 외 → D, <15+하락 → OFF)
  OFF: 비활성

State (symbol → dict):
  highest:       포지션 진입 후 최고가
  entry_price:   매수 평단
  in_position:   현재 보유 중 여부
"""
from __future__ import annotations

import os
from typing import Optional

import pandas as pd

from ..indicators.atr import atr_from_ohlcv
from ..indicators.adx import adx as compute_adx_series
from ..strategy.ma_cross import Decision, MACrossSignal


# 종목별 상태 저장 (runner.py 에서 import 해서 사용)
_trailing_state: dict[str, dict] = {}


def get_trailing_mode(symbol: str) -> str:
    """env vars 에서 종목별 모드 조회.

    우선순위:
      1. TRAILING_STOP_MODE_<symbol>  (예: TRAILING_STOP_MODE_005930)
      2. TRAILING_STOP_MODE_DEFAULT
      3. "OFF"
    """
    key = f"TRAILING_STOP_MODE_{symbol}"
    val = os.environ.get(key)
    if val:
        return val.strip().upper()
    return os.environ.get("TRAILING_STOP_MODE_DEFAULT", "OFF").strip().upper()


def reset_state(symbol: str) -> None:
    """포지션 종료 시 호출."""
    _trailing_state.pop(symbol, None)


def _compute_adx_value(ohlcv_list: list[dict], period: int = 14) -> float:
    """ohlcv 리스트(최신 → 과거) 에서 최근 ADX 값."""
    if len(ohlcv_list) < period * 2:
        return float("nan")
    rev = list(reversed(ohlcv_list))  # 과거→최신
    h = pd.Series([row["high"] for row in rev])
    l = pd.Series([row["low"] for row in rev])
    c = pd.Series([row["close"] for row in rev])
    s = compute_adx_series(h, l, c, period=period)
    last = s.iloc[-1] if len(s) > 0 else float("nan")
    return float(last) if pd.notna(last) else float("nan")


def _calc_stop_for_mode(
    mode: str,
    avg_price: float,
    highest: float,
    last_price: float,
    atr_val: float,
) -> tuple[float, str]:
    """주어진 모드의 trailing stop 가격 계산.

    반환: (stop_price, reason). stop_price=0 이면 비활성.
    """
    highest_pct = (highest - avg_price) / avg_price * 100

    if mode == "A":
        # Fixed: 수익 ≥1.5% 도달 시 최고가 × 0.995 (또는 BE)
        if highest_pct >= 1.5:
            stop = max(avg_price, highest * 0.995)
            return stop, f"A(fixed, hi={highest_pct:.2f}%)"
        return 0.0, ""

    if mode == "B":
        # ATR: 수익 ≥ ATR×3/가격 % 도달 시 최고가 - ATR×2.5
        if atr_val <= 0:
            return 0.0, ""
        activation_pct = (atr_val * 3) / avg_price * 100
        if highest_pct >= activation_pct:
            stop = max(avg_price, highest - atr_val * 2.5)
            return stop, f"B(atr, hi={highest_pct:.2f}%, act>={activation_pct:.2f}%)"
        return 0.0, ""

    if mode == "C":
        # Step: 단계별 락인
        if highest_pct >= 5.0:
            return avg_price * 1.030, f"C(step+3.0%, hi={highest_pct:.2f}%)"
        if highest_pct >= 3.0:
            return avg_price * 1.015, f"C(step+1.5%, hi={highest_pct:.2f}%)"
        if highest_pct >= 1.5:
            return avg_price, f"C(step BE, hi={highest_pct:.2f}%)"
        return 0.0, ""

    if mode == "D":
        # Hybrid: ATR 트레일 + 단계 락인 보장
        if atr_val <= 0 or highest_pct < 1.0:
            return 0.0, ""
        atr_stop = highest - atr_val * 2.5
        if highest_pct >= 5.0:
            min_lock = avg_price * 1.030
        elif highest_pct >= 3.0:
            min_lock = avg_price * 1.015
        elif highest_pct >= 1.5:
            min_lock = avg_price
        else:
            min_lock = 0.0
        stop = max(min_lock, atr_stop, avg_price * 0.999)
        return stop, f"D(hybrid, hi={highest_pct:.2f}%, atr_stop={atr_stop:.0f})"

    return 0.0, ""


def check_trailing_stop(
    symbol: str,
    last_price: float,
    position_qty: int,
    avg_price: float,
    ohlcv_list: list[dict],
    atr_period: int = 14,
) -> Optional[Decision]:
    """매 틱 호출. trailing stop 발동 시 SELL Decision 반환, 아니면 None.

    상태 자동 업데이트(highest 추적). 포지션 없으면 상태 리셋.
    """
    # 포지션 종료 → 상태 리셋
    if position_qty <= 0 or avg_price <= 0:
        reset_state(symbol)
        return None

    # 모드 결정
    mode = get_trailing_mode(symbol)
    if mode == "OFF":
        return None

    # 상태 초기화/조회
    state = _trailing_state.get(symbol)
    if state is None or not state.get("in_position"):
        state = {"highest": avg_price, "entry_price": avg_price, "in_position": True}
        _trailing_state[symbol] = state

    # 최고가 갱신
    state["highest"] = max(state["highest"], last_price)
    highest = state["highest"]

    # ATR 계산 (B/D 모드 또는 AUTO에서 필요)
    atr_val = 0.0
    if mode in ("B", "D", "AUTO"):
        atr_val = atr_from_ohlcv(list(reversed(ohlcv_list)), period=atr_period)

    # AUTO: ADX 기반 모드 선택
    actual_mode = mode
    auto_reason = ""
    if mode == "AUTO":
        adx_val = _compute_adx_value(ohlcv_list, period=14)
        if pd.isna(adx_val):
            return None  # ADX 계산 불가 → 비활성
        if adx_val > 25:
            actual_mode = "A"
            auto_reason = f"AUTO→A(adx={adx_val:.1f})"
        elif adx_val < 15 and last_price < avg_price:
            return None  # 약세장 → 비활성
        else:
            actual_mode = "D"
            auto_reason = f"AUTO→D(adx={adx_val:.1f})"

    # Stop 계산
    stop_price, reason_str = _calc_stop_for_mode(
        actual_mode, avg_price, highest, last_price, atr_val
    )

    if stop_price <= 0:
        return None  # 아직 trailing 활성화 안 됨

    # 발동 검사
    if last_price <= stop_price:
        profit_pct = (last_price - avg_price) / avg_price * 100
        highest_pct = (highest - avg_price) / avg_price * 100
        full_reason = f"trailing-stop {reason_str}"
        if auto_reason:
            full_reason = f"{auto_reason} → {full_reason}"
        return Decision(
            MACrossSignal.SELL,
            full_reason,
            meta={
                "kind": "trailing_stop",
                "mode": actual_mode,
                "auto_selected": mode == "AUTO",
                "trailing_stop": round(stop_price, 2),
                "highest": round(highest, 2),
                "highest_pct": round(highest_pct, 2),
                "profit_pct": round(profit_pct, 2),
                "last_price": last_price,
                "avg_price": avg_price,
            },
        )
    return None


def get_state_snapshot(symbol: str) -> dict:
    """디버깅/로그용 상태 조회."""
    s = _trailing_state.get(symbol)
    if not s:
        return {"in_position": False}
    return dict(s)
