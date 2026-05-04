"""Supertrend 전환 누락 수정 + DailyContext 동적 임계값 테스트."""
import numpy as np
import pandas as pd
import pytest

from stock_bot.strategy.supertrend import decide_supertrend
from stock_bot.strategy.daily_context import decide_daily_context
from stock_bot.strategy.ma_cross import MACrossSignal


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _make_ohlcv(closes: list[float]) -> pd.DataFrame:
    """종가 리스트로 OHLCV DataFrame 생성 (고/저는 종가 ±0.5%)."""
    c = np.array(closes, dtype=float)
    return pd.DataFrame({
        "high":   c * 1.005,
        "low":    c * 0.995,
        "close":  c,
        "volume": np.ones(len(c)) * 1000,
    })


def _make_dc_ohlcv(last_price: float, rows: int = 30) -> pd.DataFrame:
    """DailyContext용 단순 OHLCV (종가 고정)."""
    c = np.full(rows, last_price)
    return pd.DataFrame({
        "high": c * 1.002, "low": c * 0.998,
        "close": c, "volume": np.ones(rows) * 1000,
    })


# ── ST 전환 누락 수정 테스트 ──────────────────────────────────────────────────

class TestSupertrendTransition:

    def _make_bearish_then_bullish(self, period: int = 7) -> pd.DataFrame:
        """하락추세 → 상승 전환이 명확히 발생하는 캔들 시퀀스."""
        # 앞부분 하락, 뒷부분 급반등
        down = list(np.linspace(230_000, 220_000, 20))
        up   = list(np.linspace(220_000, 235_000, 15))
        return _make_ohlcv(down + up)

    def test_transition_detected_with_prev_known(self):
        """prev_known_direction=-1 제공 시 하락→상승 전환 BUY 발동."""
        df = self._make_bearish_then_bullish()
        # prev_known_direction=-1 (이전 틱이 하락추세였음을 명시)
        d = decide_supertrend(df, period=7, multiplier=2.5,
                              position_qty=0, prev_known_direction=-1)
        assert d.signal == MACrossSignal.BUY, (
            f"상승 전환 시 BUY여야 함, 실제: {d.signal} ({d.reason})"
        )

    def test_no_transition_when_already_bullish(self):
        """이전 틱도 상승(prev_known=1)이면 전환 BUY 없음."""
        df = self._make_bearish_then_bullish()
        d = decide_supertrend(df, period=7, multiplier=2.5,
                              position_qty=0, prev_known_direction=1)
        assert d.signal != MACrossSignal.BUY or "stop-loss" in d.reason, (
            f"이미 상승추세면 BUY 전환 신호 없어야 함, 실제: {d.signal} ({d.reason})"
        )

    def test_prev_none_falls_back_to_candle_comparison(self):
        """prev_known_direction=None이면 기존 direction[-2] 비교 방식 사용."""
        df = self._make_bearish_then_bullish()
        # 예외 없이 실행되면 OK (기존 동작 유지)
        d = decide_supertrend(df, period=7, multiplier=2.5,
                              position_qty=0, prev_known_direction=None)
        assert d.signal in (MACrossSignal.BUY, MACrossSignal.HOLD)

    def test_sell_transition_with_position(self):
        """prev_known=1(상승) → 현재 하락 전환 시 포지션 있으면 SELL."""
        down = list(np.linspace(235_000, 220_000, 35))
        df = _make_ohlcv(down)
        d = decide_supertrend(df, period=7, multiplier=2.5,
                              position_qty=10, avg_price=230_000,
                              prev_known_direction=1)
        assert d.signal == MACrossSignal.SELL, (
            f"하락 전환 + 포지션 있으면 SELL이어야 함, 실제: {d.signal} ({d.reason})"
        )


# ── DailyContext 동적 임계값 테스트 ───────────────────────────────────────────

class TestDailyContextDynamic:

    BASE_AVG   = 225_000.0
    PREV_CLOSE = 228_000.0   # 전일종가
    PREV_HIGH  = 229_000.0   # 전일고가

    def _dc(self, last_price: float, bullish: bool | None,
            profit_gate: float = 1.5, pdc: float = 1.5):
        ohlcv = _make_dc_ohlcv(last_price)
        return decide_daily_context(
            ohlcv_df=ohlcv,
            position_qty=10,
            avg_price=self.BASE_AVG,
            entry_date="2026-05-03",          # 전일 진입 → gate1 통과
            prev_day_high=self.PREV_HIGH,
            prev_day_close=self.PREV_CLOSE,
            profit_gate_pct=profit_gate,
            pdc_pct=pdc,
            supertrend_bullish=bullish,
            trend_bonus=0.5,
        )

    # ── 상승추세 (임계값 +0.5%p) ─────────────────────────────────────────────

    def test_bullish_blocks_early_sell(self):
        """상승추세 시 pdc 2.0%로 상향 → 1.5% 수익에서 매도 안 함."""
        # 전일종가 228,000 × 1.015 = 231,420 → 아래 가격이면 기본값엔 팔리지만 상승추세엔 안 팔림
        price = int(self.PREV_CLOSE * 1.017)  # +1.7% → 기본 1.5%는 통과, 상승추세 2.0%는 미달
        d = self._dc(price, bullish=True)
        assert d.signal == MACrossSignal.HOLD, (
            f"상승추세 중 +1.7%에선 HOLD여야 함 (임계 2.0%), 실제: {d.signal} ({d.reason})"
        )

    def test_bullish_sells_above_raised_threshold(self):
        """상승추세 시 2.0% 초과 시 SELL."""
        price = int(self.PREV_CLOSE * 1.021)  # +2.1% → 2.0% 초과
        d = self._dc(price, bullish=True)
        assert d.signal == MACrossSignal.SELL, (
            f"상승추세 중 +2.1%에선 SELL이어야 함, 실제: {d.signal} ({d.reason})"
        )

    # ── 하락/중립추세 (임계값 기본 1.5%) ─────────────────────────────────────

    def test_neutral_sells_at_base_threshold(self):
        """하락/중립 추세 시 1.5% 초과하면 SELL."""
        price = int(self.PREV_CLOSE * 1.016)  # +1.6%
        d = self._dc(price, bullish=False)
        assert d.signal == MACrossSignal.SELL, (
            f"중립추세 +1.6%에서 SELL이어야 함, 실제: {d.signal} ({d.reason})"
        )

    def test_neutral_holds_below_threshold(self):
        """하락/중립 추세 시 1.5% 미달이면 HOLD."""
        price = int(self.PREV_CLOSE * 1.014)  # +1.4%
        d = self._dc(price, bullish=False)
        assert d.signal == MACrossSignal.HOLD, (
            f"중립추세 +1.4%에서 HOLD여야 함, 실제: {d.signal} ({d.reason})"
        )

    def test_profit_gate_raised_in_bullish(self):
        """상승추세 시 profit_gate도 +0.5%p 상향 → gate2 미달이면 HOLD."""
        # avg_price=225,000, profit_gate 기본 1.5% → 상승추세 2.0%
        # 현재가 225,000 × 1.019 = 229,275 → 수익 +1.9%, gate 2.0% 미달
        price = int(self.BASE_AVG * 1.019)
        d = self._dc(price, bullish=True)
        assert d.signal == MACrossSignal.HOLD, (
            f"상승추세 profit_gate 2.0% 미달 시 HOLD여야 함, 실제: {d.signal} ({d.reason})"
        )

    def test_none_bullish_uses_base(self):
        """supertrend_bullish=None이면 기본값(1.5%) 적용."""
        price = int(self.PREV_CLOSE * 1.016)  # +1.6%
        d = self._dc(price, bullish=None)
        assert d.signal == MACrossSignal.SELL, (
            f"bullish=None 시 기본 1.5% 적용 → +1.6%에서 SELL이어야 함, 실제: {d.signal} ({d.reason})"
        )
