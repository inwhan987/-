"""백테스트 엔진 — 전략별 수익 지표 계산.

수수료 모델 (코스피 기준):
  매수: 0.015% (증권사)
  매도: 0.015% (증권사) + 0.18% (증권거래세) = 0.195%
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

BUY_COMM = 0.00015   # 0.015%
SELL_COMM = 0.00195  # 0.195%

# 5분봉 기준 연환산 계수: 78봉/일 × 252거래일
ANNUALIZE = np.sqrt(78 * 252)


@dataclass
class Trade:
    entry_bar: int
    entry_price: float
    exit_bar: int
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float


@dataclass
class BacktestResult:
    strategy: str
    total_return_pct: float
    win_rate: float
    max_drawdown_pct: float
    sharpe: float
    trades: int
    profit_factor: float
    avg_hold_bars: float
    raw_trades: list[Trade] = field(default_factory=list)


# signal 함수 타입: (df_slice, position_qty, avg_price, stop_loss_pct) -> "buy"|"sell"|"hold"
SignalFn = Callable[[pd.DataFrame, int, float, float], str]


def run_strategy(
    df: pd.DataFrame,
    signal_fn: SignalFn,
    strategy_name: str,
    initial_cash: float = 10_000_000,
    stop_loss_pct: float = 5.0,
) -> BacktestResult:
    """단일 전략 백테스트 실행.

    df 컬럼: open, high, low, close, volume (소문자).
    시그널은 봉 종가에서 계산하고 동일 봉 종가에 체결 (단순화).
    """
    closes = df["close"].values
    n = len(df)

    cash = initial_cash
    position = 0
    avg_price = 0.0
    entry_bar = 0
    trades: list[Trade] = []
    equity = np.empty(n)

    for i in range(n):
        df_slice = df.iloc[: i + 1]
        try:
            sig = signal_fn(df_slice, position, avg_price, stop_loss_pct)
        except Exception:
            sig = "hold"

        price = closes[i]

        if sig == "buy" and position == 0 and cash > price:
            qty = int(cash * 0.95 / (price * (1 + BUY_COMM)))
            if qty > 0:
                cash -= qty * price * (1 + BUY_COMM)
                position = qty
                avg_price = price
                entry_bar = i

        elif sig == "sell" and position > 0:
            proceeds = position * price * (1 - SELL_COMM)
            buy_cost = position * avg_price * (1 + BUY_COMM)
            pnl = proceeds - buy_cost
            pnl_pct = (price / avg_price - 1) * 100 - (BUY_COMM + SELL_COMM) * 100
            trades.append(Trade(entry_bar, avg_price, i, price, position, pnl, pnl_pct))
            cash += proceeds
            position = 0
            avg_price = 0.0

        equity[i] = cash + position * price

    # 미청산 포지션 마지막 가격으로 강제 청산
    if position > 0:
        last_price = closes[-1]
        proceeds = position * last_price * (1 - SELL_COMM)
        buy_cost = position * avg_price * (1 + BUY_COMM)
        pnl = proceeds - buy_cost
        pnl_pct = (last_price / avg_price - 1) * 100
        trades.append(Trade(entry_bar, avg_price, n - 1, last_price, position, pnl, pnl_pct))
        cash += proceeds
        equity[-1] = cash

    final_equity = float(equity[-1]) if n > 0 else initial_cash
    total_return_pct = (final_equity - initial_cash) / initial_cash * 100

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    peak = np.maximum.accumulate(equity)
    drawdown = np.where(peak > 0, (peak - equity) / peak * 100, 0.0)
    max_drawdown_pct = float(np.max(drawdown))

    rets = np.diff(equity) / equity[:-1] if n > 1 else np.array([])
    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * ANNUALIZE)
    else:
        sharpe = 0.0

    avg_hold = float(np.mean([t.exit_bar - t.entry_bar for t in trades])) if trades else 0.0

    return BacktestResult(
        strategy=strategy_name,
        total_return_pct=total_return_pct,
        win_rate=win_rate,
        max_drawdown_pct=max_drawdown_pct,
        sharpe=sharpe,
        trades=len(trades),
        profit_factor=profit_factor,
        avg_hold_bars=avg_hold,
        raw_trades=trades,
    )
