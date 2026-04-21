"""포지션 사이징 전략.

3가지 방식:
  1) fixed       — 고정 금액 (기존 방식, 하위호환)
  2) fraction    — 계좌 총액의 고정 비율
  3) atr         — 변동성 기반. 한 번에 계좌의 X% 만 리스크

반환:
  SizingResult(quantity, stop_distance, method, note)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SizingResult:
    quantity: int
    stop_distance: float  # 가격 단위. 0 이면 전략에 손절 정보 주지 않음
    method: str
    note: str = ""


def fixed_amount(cash_per_trade: int, price: float) -> SizingResult:
    qty = max(1, int(cash_per_trade // max(price, 1)))
    return SizingResult(quantity=qty, stop_distance=0.0, method="fixed")


def fixed_fraction(account_value: float, fraction: float, price: float) -> SizingResult:
    """계좌 × fraction 만큼 사용 (복리 효과 있음)."""
    budget = max(0.0, account_value * fraction)
    qty = max(1, int(budget // max(price, 1)))
    return SizingResult(
        quantity=qty, stop_distance=0.0, method="fraction",
        note=f"budget={budget:,.0f} ({fraction*100:.1f}%)",
    )


def atr_sizing(
    account_value: float,
    risk_pct: float,
    atr_value: float,
    stop_multiplier: float,
    price: float,
    max_position_pct: float = 30.0,
) -> SizingResult:
    """ATR 기반 사이징.

    한 번에 잃을 수 있는 최대 금액 = 계좌 × risk_pct / 100
    손절 거리                     = ATR × stop_multiplier
    수량                          = 리스크금액 / 손절거리

    max_position_pct: 한 종목이 계좌의 이 비율을 넘지 않게 상한.
    """
    if atr_value <= 0 or price <= 0:
        return SizingResult(0, 0.0, "atr", "invalid atr/price -> skip")

    risk_amount = account_value * (risk_pct / 100.0)
    stop_distance = atr_value * stop_multiplier
    raw_qty = int(risk_amount // stop_distance) if stop_distance > 0 else 0

    # 포지션 상한
    position_cap_qty = int((account_value * max_position_pct / 100.0) // price)
    qty = max(0, min(raw_qty, position_cap_qty))

    if qty == 0:
        return SizingResult(
            0, stop_distance, "atr",
            f"qty=0 (risk={risk_amount:,.0f}, stop={stop_distance:,.0f}, cap={position_cap_qty})",
        )
    return SizingResult(
        qty, stop_distance, "atr",
        f"risk={risk_amount:,.0f} stop={stop_distance:,.0f}/share -> {qty}주",
    )
