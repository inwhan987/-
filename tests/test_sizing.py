"""포지션 사이징 테스트."""
from __future__ import annotations

from stock_bot.sizing import atr_sizing, fixed_amount, fixed_fraction


def test_fixed_amount_divides_cash():
    r = fixed_amount(cash_per_trade=1_000_000, price=50_000)
    assert r.method == "fixed"
    assert r.quantity == 20


def test_fixed_amount_rounds_down():
    r = fixed_amount(cash_per_trade=100_000, price=30_000)
    assert r.quantity == 3


def test_fixed_fraction_uses_account_ratio():
    r = fixed_fraction(account_value=10_000_000, fraction=0.02, price=50_000)
    # budget = 200k, qty = 4
    assert r.method == "fraction"
    assert r.quantity == 4


def test_atr_sizing_risk_controls_quantity():
    # 계좌 10M, 리스크 1% = 100k 리스크 허용
    # ATR=1000 * 2배 = 2000 stop_distance
    # qty = 100_000 / 2000 = 50
    r = atr_sizing(
        account_value=10_000_000,
        risk_pct=1.0,
        atr_value=1000,
        stop_multiplier=2.0,
        price=50_000,
        max_position_pct=30.0,
    )
    assert r.method == "atr"
    assert r.quantity == 50
    assert r.stop_distance == 2000


def test_atr_sizing_respects_max_position_cap():
    # 작은 ATR -> 매우 큰 수량 계산되지만 포지션 상한 30% 에 걸려야 한다
    # cap qty = (10M * 30%) / 50k = 60
    r = atr_sizing(
        account_value=10_000_000,
        risk_pct=5.0,
        atr_value=50,
        stop_multiplier=2.0,
        price=50_000,
        max_position_pct=30.0,
    )
    assert r.quantity == 60


def test_atr_sizing_zero_atr_returns_zero():
    r = atr_sizing(
        account_value=10_000_000,
        risk_pct=1.0,
        atr_value=0,
        stop_multiplier=2.0,
        price=50_000,
    )
    assert r.quantity == 0
    assert "skip" in r.note
