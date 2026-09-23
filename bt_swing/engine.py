# -*- coding: utf-8 -*-
"""포트폴리오 체결 시뮬레이터.

체결 규칙 (미래참조 차단이 핵심)
--------------------------------
- 신호는 t일 종가 확정 후 생성 → 체결은 t+1일 '시가'
- 청산은 보유 중 매일 OHLC로 판정, 같은 날 손절·익절이 겹치면 손절 우선(보수적)
- 시가가 전일 종가 대비 ±29.5% 밖이거나 고가==저가(점상/점하한)면 체결 불가
- 매수/매도 각각 수수료·세금·슬리피지 반영
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .config import BacktestCfg


@dataclass
class Position:
    ticker: str
    strategy: str
    entry_date: pd.Timestamp
    entry_px: float
    shares: int
    stop_px: float
    tp_px: float
    score: float
    peak: float
    bars: int = 0
    trail_on: bool = False
    sector: str = "UNKNOWN"


@dataclass
class Trade:
    ticker: str
    strategy: str
    entry_date: str
    exit_date: str
    entry_px: float
    exit_px: float
    shares: int
    hold_days: int
    score: float
    reason: str
    ret_gross: float
    ret_net: float
    pnl: float


def _tradable(row: pd.Series, prev_close: float, limit_move: float) -> bool:
    """그날 시가에 체결 가능한지.

    시가 시점에 알 수 있는 정보만 쓴다(전일 종가 대비 갭). 당일 고가/저가로
    거르면 미래참조가 되므로 쓰지 않는다.
    """
    o = row["open"]
    if not np.isfinite(o) or o <= 0:
        return False
    if prev_close and prev_close > 0:
        if abs(o / prev_close - 1.0) >= limit_move:
            return False
    return True


def _entry_gate(prev_row: pd.Series, cfg: BacktestCfg) -> str | None:
    """진입 직전 게이트. 통과하면 None, 막히면 사유 문자열.

    신호일(prev) 종가까지의 정보만 쓴다. 점수는 전 종목에 매기되
    '실제로 체결 가능한가'는 여기서 판정한다.
    """
    v = prev_row.get("value_eok", np.nan)
    if cfg.entry_min_value_eok > 0:
        if not np.isfinite(v) or v < cfg.entry_min_value_eok:
            return "유동성미달"
    if cfg.entry_min_mktcap_eok > 0:
        cap = prev_row.get("mktcap_eok", np.nan)
        if not np.isfinite(cap):
            # 시총을 모르면 통과시키던 예전 동작은 위험했다. 수집이 통째로
            # 실패하면 필터 전체가 조용히 무력화돼 초소형주가 다 들어온다.
            if cfg.block_when_cap_missing:
                return "시총불명"
        elif cap < cfg.entry_min_mktcap_eok:
            return "시총미달"
    if cfg.entry_min_price > 0 and float(prev_row.get("close", 0)) < cfg.entry_min_price:
        return "가격미달"
    if cfg.entry_max_atr_pct > 0:
        a = prev_row.get("atr_pct", np.nan)
        if np.isfinite(a) and a > cfg.entry_max_atr_pct:
            return "과열"
    return None


def _exit_check(pos: Position, row: pd.Series, cfg: BacktestCfg) -> tuple[float, str] | None:
    """그날 청산되는지 판정. (체결가, 사유) 또는 None."""
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    if h == l:                       # 점상/점하한이면 청산 불가
        return None

    # 1) 손절 우선 (같은 날 익절과 겹치면 보수적으로 손절 처리)
    if o <= pos.stop_px:
        return o, "손절(갭)"
    if l <= pos.stop_px:
        return pos.stop_px, "손절"

    # 2) 익절
    if o >= pos.tp_px:
        return o, "익절(갭)"
    if h >= pos.tp_px:
        return pos.tp_px, "익절"

    # 3) 트레일링
    if cfg.exits.use_trailing:
        if not pos.trail_on and h >= pos.entry_px * (1 + cfg.exits.trail_after):
            pos.trail_on = True
        pos.peak = max(pos.peak, h)
        if pos.trail_on and c <= pos.peak * (1 - cfg.exits.trail_pct):
            return c, "트레일링"

    # 4) 타임스톱
    if pos.bars >= cfg.exits.time_stop_days:
        return c, "타임스톱"

    return None


def _stop_tp(entry_px: float, atr_pct: float, cfg: BacktestCfg) -> tuple[float, float]:
    e = cfg.exits
    if e.use_atr_stop and np.isfinite(atr_pct) and atr_pct > 0:
        sp = float(np.clip(atr_pct * e.atr_stop_mult, e.stop_loss * 0.5, e.stop_loss * 2.0))
        tp = float(np.clip(atr_pct * e.atr_tp_mult, e.take_profit * 0.5, e.take_profit * 2.0))
    else:
        sp, tp = e.stop_loss, e.take_profit
    return entry_px * (1 - sp), entry_px * (1 + tp)


def build_signal_table(
    panel: dict[str, pd.DataFrame],
    sig: dict[str, pd.DataFrame],
    strategy_name: str,
) -> pd.DataFrame:
    """모든 종목의 신호를 (date, ticker, score) 롱테이블로."""
    rows = []
    for tk, s in sig.items():
        m = s["signal"].values.astype(bool)
        if not m.any():
            continue
        rows.append(pd.DataFrame({
            "date": s.index[m],
            "ticker": tk,
            "score": s["score"].values[m],
            "strategy": strategy_name,
        }))
    if not rows:
        return pd.DataFrame(columns=["date", "ticker", "score", "strategy"])
    out = pd.concat(rows, ignore_index=True)
    return out.sort_values(["date", "score"], ascending=[True, False])


def simulate(
    panel: dict[str, pd.DataFrame],
    signals: pd.DataFrame,
    cfg: BacktestCfg,
    regime: pd.Series | None = None,
    calendar: pd.DatetimeIndex | None = None,
    sector_of=None,
) -> dict:
    """단일 전략 포트폴리오 시뮬레이션.

    sector_of: get(ticker, date) -> 업종명 을 제공하는 객체. 주면
    cfg.portfolio.max_per_sector 가 적용된다 (0이면 미적용).
    """
    if calendar is None:
        allidx = sorted({d for df in panel.values() for d in df.index})
        calendar = pd.DatetimeIndex(allidx)
    calendar = pd.DatetimeIndex(sorted(set(calendar)))

    by_date: dict[pd.Timestamp, pd.DataFrame] = {
        d: g for d, g in signals.groupby("date")
    } if len(signals) else {}

    cash = cfg.portfolio.initial_capital
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity: list[tuple[pd.Timestamp, float]] = []
    skipped = {"자금부족": 0, "체결불가": 0, "슬롯없음": 0, "레짐차단": 0}

    for i, day in enumerate(calendar):
        # ── 1. 보유 포지션 청산 판정 ────────────────────────────────
        for tk in list(positions.keys()):
            pos = positions[tk]
            df = panel.get(tk)
            if df is None or day not in df.index:
                continue
            row = df.loc[day]
            if day == pos.entry_date:
                pos.peak = max(pos.peak, row["high"])
                continue                       # 진입 당일은 청산 판정 생략(보수적)
            pos.bars += 1
            res = _exit_check(pos, row, cfg)
            if res is None:
                continue
            px, reason = res
            gross = px / pos.entry_px - 1.0
            net = gross - cfg.costs.buy_cost() - cfg.costs.sell_cost()
            proceeds = pos.shares * px * (1 - cfg.costs.sell_cost())
            cash += proceeds
            trades.append(Trade(
                ticker=tk, strategy=pos.strategy,
                entry_date=pos.entry_date.strftime("%Y-%m-%d"),
                exit_date=day.strftime("%Y-%m-%d"),
                entry_px=float(pos.entry_px), exit_px=float(px),
                shares=pos.shares, hold_days=pos.bars, score=float(pos.score),
                reason=reason, ret_gross=float(gross), ret_net=float(net),
                pnl=float(pos.shares * pos.entry_px * net),
            ))
            del positions[tk]

        # ── 2. 전일 신호로 오늘 시가 진입 ──────────────────────────
        if i > 0:
            prev = calendar[i - 1]
            cand = by_date.get(prev)
            if cand is not None and len(cand):
                size_mult = 1.0
                if regime is not None and cfg.regime.enabled:
                    r = regime.reindex([prev]).iloc[0] if prev in regime.index else np.nan
                    if r == 0:
                        size_mult = cfg.regime.below_size_mult
                if size_mult <= 0:
                    skipped["레짐차단"] += len(cand)
                else:
                    added = 0
                    for _, s in cand.iterrows():
                        if added >= cfg.portfolio.max_new_per_day:
                            break
                        if len(positions) >= cfg.portfolio.max_positions:
                            skipped["슬롯없음"] += 1
                            break
                        tk = s["ticker"]
                        if tk in positions:
                            continue
                        sec = "UNKNOWN"
                        if sector_of is not None and cfg.portfolio.max_per_sector > 0:
                            sec = sector_of.get(tk, prev)
                            if sec != "UNKNOWN":
                                held = sum(1 for p in positions.values()
                                           if p.sector == sec)
                                if held >= cfg.portfolio.max_per_sector:
                                    skipped["섹터상한"] = skipped.get("섹터상한", 0) + 1
                                    continue
                        df = panel.get(tk)
                        if df is None or day not in df.index or prev not in df.index:
                            skipped["체결불가"] += 1
                            continue
                        row = df.loc[day]
                        prev_row = df.loc[prev]
                        prev_close = float(prev_row["close"])
                        # 2026-09-23: 전략별 원점수 하한 (라이브 daily_scan._entry_gate 와 같은 판정)
                        _cut = cfg.entry_min_raw_by_strategy.get(str(s.get("strategy", "")).upper())
                        if _cut is not None and float(s.get("score", 0.0)) < _cut:
                            skipped["점수미달"] = skipped.get("점수미달", 0) + 1
                            continue
                        block = _entry_gate(prev_row, cfg)
                        if block:
                            skipped[block] = skipped.get(block, 0) + 1
                            continue
                        if not _tradable(row, prev_close, cfg.limit_move):
                            skipped["체결불가"] += 1
                            continue
                        entry_px = float(row["open"]) * (1 + cfg.costs.buy_cost())
                        # 사이징은 '전일 종가' 기준 평가액으로. 당일 종가를 쓰면 미래참조.
                        equity_now = cash + sum(
                            p.shares * float(panel[t].loc[prev, "close"])
                            for t, p in positions.items()
                            if prev in panel[t].index
                        )
                        alloc = equity_now * cfg.portfolio.position_pct * size_mult
                        shares = int(math.floor(alloc / entry_px)) if entry_px > 0 else 0
                        if shares <= 0 or shares * entry_px > cash:
                            skipped["자금부족"] += 1
                            continue
                        # 주문 규모가 그날 거래대금 대비 너무 크면 내 주문이
                        # 가격을 밀어버린다. 체결됐다고 치면 안 된다.
                        if cfg.max_order_share_of_value > 0:
                            dv = float(row.get("value", np.nan))
                            if np.isfinite(dv) and dv > 0:
                                if (shares * entry_px) / dv > cfg.max_order_share_of_value:
                                    skipped["주문비중초과"] = skipped.get("주문비중초과", 0) + 1
                                    continue
                        cash -= shares * entry_px
                        atr_pct = float(df.loc[prev, "atr_pct"]) if "atr_pct" in df.columns else np.nan
                        raw_px = float(row["open"])
                        stop_px, tp_px = _stop_tp(raw_px, atr_pct, cfg)
                        positions[tk] = Position(
                            ticker=tk, strategy=str(s["strategy"]), entry_date=day,
                            entry_px=raw_px, shares=shares, stop_px=stop_px, tp_px=tp_px,
                            score=float(s["score"]), peak=float(row["high"]),
                            sector=sec,
                        )
                        added += 1

        # ── 3. 일일 평가액 ─────────────────────────────────────────
        mv = 0.0
        for tk, p in positions.items():
            df = panel.get(tk)
            if df is not None and day in df.index:
                mv += p.shares * float(df.loc[day, "close"])
            else:
                mv += p.shares * p.entry_px
        equity.append((day, cash + mv))

    eq = pd.Series(dict(equity)).sort_index()
    tr = pd.DataFrame([asdict(t) for t in trades])
    return {"equity": eq, "trades": tr, "skipped": skipped,
            "open_positions": len(positions)}


def make_regime(index_df: pd.DataFrame, ma: int = 200) -> pd.Series:
    """지수 종가가 MA 위면 1, 아래면 0."""
    m = index_df["close"].rolling(ma, min_periods=ma).mean()
    return (index_df["close"] > m).astype(float).fillna(1.0)
