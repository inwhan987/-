"""뉴스 감성 기반 전략.

최근 N시간 기사의 평균 감성 점수를 사용:
  score >= buy_threshold  AND  기사 수 >= min_articles  ->  BUY
  score <= sell_threshold AND  기사 수 >= min_articles  ->  SELL
손절 규칙은 다른 전략과 동일.
"""
from __future__ import annotations

from .ma_cross import Decision, MACrossSignal


def decide_news(
    recent_close: float,
    sentiment_score: float,
    article_count: int,
    buy_threshold: float = 0.3,
    sell_threshold: float = -0.3,
    min_articles: int = 3,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
) -> Decision:
    if position_qty > 0 and avg_price > 0:
        loss_pct = (recent_close - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(MACrossSignal.SELL, f"stop-loss {loss_pct:.2f}%")

    if article_count < min_articles:
        return Decision(
            MACrossSignal.HOLD, f"news sparse ({article_count} articles, score={sentiment_score:+.2f})"
        )

    if position_qty == 0 and sentiment_score >= buy_threshold:
        return Decision(
            MACrossSignal.BUY, f"news bullish {sentiment_score:+.2f} ({article_count} articles)"
        )
    if position_qty > 0 and sentiment_score <= sell_threshold:
        return Decision(
            MACrossSignal.SELL, f"news bearish {sentiment_score:+.2f} ({article_count} articles)"
        )
    return Decision(
        MACrossSignal.HOLD, f"news neutral {sentiment_score:+.2f} ({article_count} articles)"
    )
