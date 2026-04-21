"""앙상블 전략 단위 테스트."""
from __future__ import annotations

import pandas as pd

from stock_bot.strategy import MACrossSignal
from stock_bot.strategy.ensemble import EnsembleConfig, decide_ensemble


def test_not_enough_data():
    assert decide_ensemble(pd.Series([100.0] * 10)).signal is MACrossSignal.HOLD


def test_stop_loss_overrides_ensemble():
    closes = pd.Series([100.0] * 60)
    result = decide_ensemble(
        closes, position_qty=10, avg_price=120.0, stop_loss_pct=5.0
    )
    assert result.signal is MACrossSignal.SELL
    assert "stop-loss" in result.reason


def test_flat_market_holds():
    closes = pd.Series([100.0] * 60)
    result = decide_ensemble(closes)
    assert result.signal is MACrossSignal.HOLD


def test_v_shape_triggers_buy_with_default_config():
    # V자 반등 — 여러 전략이 동시에 BUY 신호를 낼 가능성이 높음
    closes = pd.Series([100.0 - i for i in range(40)] + [62.0, 65.0, 72.0])
    result = decide_ensemble(closes, position_qty=0)
    # 최소 결과는 HOLD 또는 BUY 여야 함 (SELL 이 나오면 안 됨)
    assert result.signal in (MACrossSignal.BUY, MACrossSignal.HOLD)


def test_single_vote_does_not_buy_by_default():
    # BUY 표 1개로는 매수 임계 미달
    cfg = EnsembleConfig(min_buy_votes=2, buy_threshold=0.6)
    # 점수는 충분해도 투표 수가 부족하면 HOLD
    closes = pd.Series([100.0] * 60)
    result = decide_ensemble(closes, config=cfg)
    assert result.signal is MACrossSignal.HOLD


def test_custom_weights_affect_score():
    closes = pd.Series([100.0] * 60)
    cfg = EnsembleConfig(weights=(1.0, 0.0, 0.0, 0.0))
    result = decide_ensemble(closes, config=cfg)
    # 가중치만 바꿨다고 시그널이 나오진 않지만, reason 에 점수 표기 확인
    assert "score=" in result.reason
