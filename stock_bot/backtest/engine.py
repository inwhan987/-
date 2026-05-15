"""백테스트 엔진 — 전략별 수익 지표 계산.

수수료 모델 (코스피 기준):
  매수: 0.015% (증권사)
  매도: 0.015% (증권사) + 0.18% (증권거래세) = 0.195%
"""
from __future__ import annotations

import inspect
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


# signal 함수 타입: (df_slice, position_qty, avg_price, stop_loss_pct[, ctx]) -> "buy"|"sell"|"hold"
# ctx 는 선택적 dict: {"entry_date": str, "prev_day_high": float, "prev_day_close": float}
SignalFn = Callable[[pd.DataFrame, int, float, float], str]


def _accepts_ctx(fn: Callable) -> bool:
    """signal_fn 이 ctx 키워드 인수를 받는지 inspect 로 확인."""
    try:
        sig = inspect.signature(fn)
        return "ctx" in sig.parameters
    except (ValueError, TypeError):
        return False


def _call_signal(fn: Callable, df_slice: pd.DataFrame, position: int,
                 avg_price: float, stop_loss_pct: float, ctx: dict) -> str:
    """ctx 수용 여부에 따라 signal_fn 을 호출. 예외 시 'hold' 반환."""
    try:
        if _accepts_ctx(fn):
            return fn(df_slice, position, avg_price, stop_loss_pct, ctx=ctx)
        return fn(df_slice, position, avg_price, stop_loss_pct)
    except Exception:
        return "hold"


def _build_prev_day_data(df: pd.DataFrame) -> dict[str, dict[str, float]]:
    """날짜별 전일 고가·종가 맵을 미리 계산.

    Returns: {date_str: {"high": float, "close": float}}
             (첫째 날은 데이터 없으므로 해당 날짜 키 자체가 없음)
    """
    dates = df.index.date
    groups: dict = {}
    for date, row in zip(dates, df.itertuples(index=False)):
        d = str(date)
        if d not in groups:
            groups[d] = {"high": row.high, "close": row.close}
        else:
            if row.high > groups[d]["high"]:
                groups[d]["high"] = row.high
            groups[d]["close"] = row.close  # 마지막 값이 종가

    sorted_dates = sorted(groups.keys())
    prev_day_data: dict[str, dict[str, float]] = {}
    for i in range(1, len(sorted_dates)):
        today = sorted_dates[i]
        yesterday = sorted_dates[i - 1]
        prev_day_data[today] = {
            "high": groups[yesterday]["high"],
            "close": groups[yesterday]["close"],
        }
    return prev_day_data


def run_strategy(
    df: pd.DataFrame,
    signal_fn: SignalFn,
    strategy_name: str,
    initial_cash: float = 10_000_000,
    stop_loss_pct: float = 5.0,
    # ── 추가매수/안전장치 옵션 (실전 러너와 동일하게) ─────────────
    enable_add_buy: bool = False,
    add_buy_fraction: float = 0.2,            # 계좌의 N%로 추가매수
    add_buy_max_count: int = 2,               # 하루 최대 추가매수 횟수
    add_buy_max_position_pct: float = 0.80,   # 계좌 N% 이상이면 추가매수 거부
    inherit_initial_stop: bool = False,       # 추가매수 후에도 초기 stop_pct 유지
    post_stoploss_cooldown_min: int = 0,      # 손절 후 N분간 재진입 차단
    initial_position_fraction: float = 0.95,  # 신규 진입 계좌 비율 (기존 호환: 0.95)
    bar_minutes: int = 5,                     # 봉 간격 (분)
) -> BacktestResult:
    """단일 종목 백테스트 실행 (추가매수·쿨다운·손절선 잠금 지원).

    df 컬럼: open, high, low, close, volume (소문자).
    시그널은 봉 종가에서 계산하고 동일 봉 종가에 체결.

    signal_fn 이 ctx 키워드를 받으면 다음 dict 를 전달한다:
      ctx = {
          "entry_date":      str | None,
          "prev_day_high":   float,
          "prev_day_close":  float,
      }

    실전 러너 (runner.py) 동작 재현:
      - enable_add_buy=True → position>0 일 때 buy 시그널이 추가매수로 처리
      - inherit_initial_stop=True → 추가매수 후에도 초기 stop_pct 유지
      - post_stoploss_cooldown_min>0 → 손절 후 N분 동안 BUY 무시
    """
    closes = df["close"].values
    n = len(df)
    dates = [str(t.date()) for t in df.index]

    prev_day_data = _build_prev_day_data(df)

    cash = initial_cash
    position = 0
    avg_price = 0.0
    entry_bar = 0
    entry_date: str | None = None
    trades: list[Trade] = []
    equity = np.empty(n)

    # 추가매수 / 안전장치 상태
    locked_stop_pct: float | None = None
    add_buy_count = 0
    add_buy_count_date: str | None = None
    last_stop_loss_bar = -10**9  # 충분히 먼 과거
    cooldown_bars = post_stoploss_cooldown_min // max(bar_minutes, 1)

    for i in range(n):
        df_slice = df.iloc[: i + 1]
        today_str = dates[i]
        ctx: dict = {
            "entry_date": entry_date,
            "prev_day_high": prev_day_data.get(today_str, {}).get("high", 0.0),
            "prev_day_close": prev_day_data.get(today_str, {}).get("close", 0.0),
        }

        # 효과적 stop_pct (잠금 적용)
        eff_stop = locked_stop_pct if (locked_stop_pct is not None and position > 0) else stop_loss_pct

        sig = _call_signal(signal_fn, df_slice, position, avg_price, eff_stop, ctx)

        price = closes[i]

        # ── 손절 후 쿨다운 체크 ───────────────────────────────────
        in_cooldown = (i - last_stop_loss_bar) < cooldown_bars

        if sig == "buy":
            # ── 신규 매수 ──
            if position == 0 and cash > price and not in_cooldown:
                # 신규 진입: 계좌의 initial_position_fraction 사용
                # (기존 동작 호환: fraction=0.40 이 기본)
                budget = cash * initial_position_fraction
                qty = int(budget / (price * (1 + BUY_COMM)))
                if qty > 0:
                    cash -= qty * price * (1 + BUY_COMM)
                    position = qty
                    avg_price = price
                    entry_bar = i
                    entry_date = today_str
                    add_buy_count = 0
                    add_buy_count_date = today_str
                    if inherit_initial_stop:
                        locked_stop_pct = stop_loss_pct  # 잠금 시작

            # ── 추가매수 ──
            elif position > 0 and enable_add_buy:
                # 하루 카운트 리셋
                if add_buy_count_date != today_str:
                    add_buy_count = 0
                    add_buy_count_date = today_str
                # 한도 체크
                position_value = position * price
                total_equity = cash + position_value
                pos_pct = position_value / total_equity if total_equity > 0 else 0
                if (add_buy_count < add_buy_max_count
                    and pos_pct < add_buy_max_position_pct):
                    # 추가매수: 계좌의 add_buy_fraction 사용
                    budget = cash * add_buy_fraction
                    add_qty = int(budget / (price * (1 + BUY_COMM)))
                    if add_qty > 0 and cash > add_qty * price * (1 + BUY_COMM):
                        cost = add_qty * price * (1 + BUY_COMM)
                        cash -= cost
                        # 가중 평균 평단가
                        new_total = position + add_qty
                        avg_price = (avg_price * position + price * add_qty) / new_total
                        position = new_total
                        add_buy_count += 1
                        # inherit_initial_stop 이면 locked_stop_pct 유지 (재설정 안 함)

        elif sig == "sell" and position > 0:
            proceeds = position * price * (1 - SELL_COMM)
            buy_cost = position * avg_price * (1 + BUY_COMM)
            pnl = proceeds - buy_cost
            pnl_pct = (price / avg_price - 1) * 100 - (BUY_COMM + SELL_COMM) * 100
            trades.append(Trade(entry_bar, avg_price, i, price, position, pnl, pnl_pct))
            cash += proceeds

            # 손절 감지: 누적 손실이 stop_pct 이상이면 cooldown 시작
            if pnl_pct <= -eff_stop:
                last_stop_loss_bar = i

            position = 0
            avg_price = 0.0
            entry_date = None
            locked_stop_pct = None
            add_buy_count = 0

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
