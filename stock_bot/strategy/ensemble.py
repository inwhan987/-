"""앙상블 전략: 4개 하위 전략의 투표 + 가중 점수 하이브리드.

매수 조건 (모두 만족):
  weighted_score >= buy_threshold
  AND  BUY 표를 던진 전략 수 >= min_buy_votes

매도 조건 (어느 하나라도 만족):
  손절 (평단 대비 stop_loss_pct 초과 손실)
  OR  weighted_score <= sell_threshold  AND  SELL 표 >= min_sell_votes

매수는 까다롭게, 매도는 빠르게 — 하방 위험 비대칭 처리.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .bollinger import decide_bollinger
from .ma_cross import Decision, MACrossSignal, decide
from .macd import decide_macd
from .rsi import decide_rsi


_SIGNAL_SCORE = {MACrossSignal.BUY: 1, MACrossSignal.HOLD: 0, MACrossSignal.SELL: -1}


@dataclass
class EnsembleConfig:
    weights: tuple[float, float, float, float] = (0.3, 0.3, 0.2, 0.2)  # ma, macd, rsi, bb
    buy_threshold: float = 0.6
    sell_threshold: float = -0.4
    min_buy_votes: int = 2
    min_sell_votes: int = 1
    # 하위 전략 파라미터 (런너에서 주입)
    short_ma: int = 5
    long_ma: int = 20
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_window: int = 20
    bb_k: float = 2.0
    # 선택적 뉴스 구성요소
    news_weight: float = 0.0  # 0 이면 뉴스 미포함
    news_sentiment: float | None = None  # 외부 주입 (-1 ~ +1)
    news_article_count: int = 0
    news_buy_threshold: float = 0.3
    news_sell_threshold: float = -0.3
    news_min_articles: int = 3


def decide_ensemble(
    closes: pd.Series,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
    config: EnsembleConfig | None = None,
) -> Decision:
    cfg = config or EnsembleConfig()
    if len(closes) < max(cfg.long_ma, cfg.macd_slow + cfg.macd_signal, cfg.bb_window) + 2:
        return Decision(MACrossSignal.HOLD, "not enough data")

    # 손절 우선
    last_price = float(closes.iloc[-1])
    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    # 각 전략의 원시 시그널 수집 (손절 규칙은 앙상블 상단에서 처리했으므로 비활성화)
    sub_decisions = [
        (
            "ma",
            decide(closes, cfg.short_ma, cfg.long_ma, position_qty, avg_price, stop_loss_pct=999),
        ),
        (
            "macd",
            decide_macd(
                closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal,
                position_qty, avg_price, stop_loss_pct=999,
            ),
        ),
        (
            "rsi",
            decide_rsi(
                closes, cfg.rsi_period, cfg.rsi_oversold, cfg.rsi_overbought,
                position_qty, avg_price, stop_loss_pct=999,
            ),
        ),
        (
            "bb",
            decide_bollinger(
                closes, cfg.bb_window, cfg.bb_k,
                position_qty, avg_price, stop_loss_pct=999,
            ),
        ),
    ]

    # 가중 점수와 투표 집계
    score = 0.0
    buy_votes = 0
    sell_votes = 0
    tags: list[str] = []
    for (name, d), w in zip(sub_decisions, cfg.weights):
        s = _SIGNAL_SCORE[d.signal]
        score += s * w
        if d.signal is MACrossSignal.BUY:
            buy_votes += 1
            tags.append(f"{name}+")
        elif d.signal is MACrossSignal.SELL:
            sell_votes += 1
            tags.append(f"{name}-")

    # 5번째 선택 요소: 뉴스 감성
    if cfg.news_weight > 0 and cfg.news_sentiment is not None and cfg.news_article_count >= cfg.news_min_articles:
        news_signal = 0
        if cfg.news_sentiment >= cfg.news_buy_threshold:
            news_signal = 1
            buy_votes += 1
            tags.append("news+")
        elif cfg.news_sentiment <= cfg.news_sell_threshold:
            news_signal = -1
            sell_votes += 1
            tags.append("news-")
        score += news_signal * cfg.news_weight

    reason = f"score={score:+.2f} votes=B{buy_votes}/S{sell_votes} [{' '.join(tags) or 'all hold'}]"

    # 매수 (까다롭게)
    if position_qty == 0 and score >= cfg.buy_threshold and buy_votes >= cfg.min_buy_votes:
        return Decision(MACrossSignal.BUY, reason)

    # 매도 (빠르게)
    if position_qty > 0 and score <= cfg.sell_threshold and sell_votes >= cfg.min_sell_votes:
        return Decision(MACrossSignal.SELL, reason)

    return Decision(MACrossSignal.HOLD, reason)
