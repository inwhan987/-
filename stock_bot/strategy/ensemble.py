"""앙상블 전략: 4개 하위 전략 투표 + 뉴스 감성 modulator.

기본 규칙 (뉴스 critical 기사 없을 때):
  매수: 4개 중 3개 이상이 BUY AND weighted_score >= buy_threshold
  매도: 2개 이상이 SELL 이거나 손절

뉴스는 투표에 참여하지 않고 다음 방식으로 영향:
  - weighted_score 에 `news_sentiment × news_weight` 합산
  - critical 기사가 있으면 "게이트 완화/강화":
      * 뉴스 >= +0.5 AND critical >= 1 → 매수 투표 요건 1 감소 (3→2)
      * 뉴스 <= -0.5 AND critical >= 1 → 매도 투표 요건 무시 (포지션 보유 시 즉시 SELL)
  - 뉴스가 강하게 반대면 (예: 매수 직전인데 뉴스 <= -0.4) 매수 거부
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
    min_buy_votes: int = 3   # 4개 중 3개 동의
    min_sell_votes: int = 2
    # 하위 전략 파라미터
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
    # 뉴스 modulator (투표 아님)
    news_weight: float = 0.3           # 뉴스 점수를 weighted_score 에 더할 비중
    news_sentiment: float | None = None
    news_article_count: int = 0
    news_critical_count: int = 0       # critical 기사 수 (LLM phrase 매칭)
    news_min_articles: int = 3
    news_veto_threshold: float = -0.4  # 이 이하면 기술적 BUY 거부
    news_escalate_buy: float = 0.5     # 이 이상 + critical 있으면 buy 요건 -1
    news_escalate_sell: float = -0.5   # 이 이하 + critical 있으면 즉시 SELL(보유 시)


def _news_usable(cfg: EnsembleConfig) -> bool:
    return (
        cfg.news_weight > 0
        and cfg.news_sentiment is not None
        and cfg.news_article_count >= cfg.news_min_articles
    )


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

    last_price = float(closes.iloc[-1])
    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(
                MACrossSignal.SELL,
                f"stop-loss {loss_pct:.2f}%",
                meta={"kind": "stop_loss", "loss_pct": loss_pct, "avg_price": avg_price, "last_price": last_price},
            )

    sub_decisions = [
        ("ma", decide(closes, cfg.short_ma, cfg.long_ma, position_qty, avg_price, stop_loss_pct=999)),
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

    score = 0.0
    buy_votes = 0
    sell_votes = 0
    tags: list[str] = []
    votes_detail: list[dict] = []
    for (name, d), w in zip(sub_decisions, cfg.weights):
        s = _SIGNAL_SCORE[d.signal]
        score += s * w
        votes_detail.append(
            {"name": name, "signal": d.signal.value, "weight": w, "reason": d.reason}
        )
        if d.signal is MACrossSignal.BUY:
            buy_votes += 1
            tags.append(f"{name}+")
        elif d.signal is MACrossSignal.SELL:
            sell_votes += 1
            tags.append(f"{name}-")

    # 뉴스 modulator
    news_bias = 0.0
    news_tag = ""
    if _news_usable(cfg):
        news_bias = cfg.news_sentiment * cfg.news_weight
        score += news_bias
        news_tag = (
            f"news={cfg.news_sentiment:+.2f}"
            f"{'*' if cfg.news_critical_count > 0 else ''}"
            f"(n={cfg.news_article_count})"
        )
        tags.append(news_tag)

    # critical 뉴스 게이트 조정
    min_buy = cfg.min_buy_votes
    min_sell = cfg.min_sell_votes
    has_critical = cfg.news_critical_count > 0

    if _news_usable(cfg) and has_critical:
        if cfg.news_sentiment >= cfg.news_escalate_buy:
            min_buy = max(2, cfg.min_buy_votes - 1)
            tags.append("news-boost-buy")
        if cfg.news_sentiment <= cfg.news_escalate_sell and position_qty > 0:
            # critical 악재 → 보유 포지션 즉시 매도
            reason = (
                f"news-critical SELL: score={score:+.2f} {news_tag} "
                f"votes=B{buy_votes}/S{sell_votes} [{' '.join(tags)}]"
            )
            return Decision(
                MACrossSignal.SELL,
                reason,
                meta={
                    "kind": "news_critical_sell",
                    "weighted_score": round(score, 4),
                    "buy_votes": buy_votes,
                    "sell_votes": sell_votes,
                    "votes": votes_detail,
                    "news_sentiment": cfg.news_sentiment,
                    "news_article_count": cfg.news_article_count,
                    "news_critical_count": cfg.news_critical_count,
                    "last_price": last_price,
                },
            )

    # 뉴스 veto: 기술적으로 BUY 신호지만 뉴스가 크게 부정
    veto_buy = (
        _news_usable(cfg)
        and cfg.news_sentiment <= cfg.news_veto_threshold
    )

    reason = f"score={score:+.2f} votes=B{buy_votes}/S{sell_votes} [{' '.join(tags) or 'all hold'}]"

    meta: dict = {
        "kind": "ensemble",
        "weighted_score": round(score, 4),
        "buy_votes": buy_votes,
        "sell_votes": sell_votes,
        "min_buy": min_buy,
        "min_sell": min_sell,
        "buy_threshold": cfg.buy_threshold,
        "sell_threshold": cfg.sell_threshold,
        "votes": votes_detail,
        "news_sentiment": cfg.news_sentiment,
        "news_article_count": cfg.news_article_count,
        "news_critical_count": cfg.news_critical_count,
        "news_usable": _news_usable(cfg),
        "news_bias": round(news_bias, 4),
        "veto_buy": veto_buy,
        "last_price": last_price,
    }

    if (
        position_qty == 0
        and score >= cfg.buy_threshold
        and buy_votes >= min_buy
        and not veto_buy
    ):
        return Decision(MACrossSignal.BUY, reason, meta={**meta, "decision": "buy"})

    if veto_buy and buy_votes >= min_buy and score >= cfg.buy_threshold:
        return Decision(
            MACrossSignal.HOLD,
            f"veto by news: {reason}",
            meta={**meta, "decision": "hold_veto"},
        )

    if position_qty > 0 and score <= cfg.sell_threshold and sell_votes >= min_sell:
        return Decision(MACrossSignal.SELL, reason, meta={**meta, "decision": "sell"})

    return Decision(MACrossSignal.HOLD, reason, meta={**meta, "decision": "hold"})
