"""뉴스 크롤러 파서 / 감성 / 저장소 / 전략 테스트."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine

from stock_bot.news import crawler, sentiment, store
from stock_bot.news.crawler import NewsItem, parse_news_html
from stock_bot.strategy import MACrossSignal, decide_news

SAMPLE_HTML = """
<html><body>
<table class="type5">
  <tr><th>제목</th><th>정보제공</th><th>날짜</th></tr>
  <tr>
    <td class="title"><a href="/item/news_read.naver?article=1">삼성전자 사상최대 실적 돌파</a></td>
    <td class="info">조선비즈</td>
    <td class="date">2026.04.21 09:30</td>
  </tr>
  <tr class="relation_tit"><td>관련뉴스</td></tr>
  <tr>
    <td class="title"><a href="https://n.news.naver.com/article/2">어닝쇼크에 삼성전자 급락</a></td>
    <td class="info">한국경제</td>
    <td class="date">2026.04.20 16:00</td>
  </tr>
</table>
</body></html>
"""


def test_parse_news_html_extracts_items():
    items = parse_news_html(SAMPLE_HTML, "005930")
    assert len(items) == 2
    assert items[0].title == "삼성전자 사상최대 실적 돌파"
    assert items[0].url.startswith("https://finance.naver.com/")
    assert items[0].publisher == "조선비즈"
    assert items[0].published_at.year == 2026
    assert items[1].title.startswith("어닝쇼크")
    assert items[1].url.startswith("https://n.news.naver.com/")


def test_parse_news_html_skips_relation_rows():
    items = parse_news_html(SAMPLE_HTML, "005930")
    assert all("관련뉴스" not in i.title for i in items)


def test_sentiment_positive_headline():
    r = sentiment.score_sentiment("삼성전자 사상최대 실적 돌파, 목표가상향")
    assert r.score > 0.3
    assert "사상최대" in r.positives or "돌파" in r.positives


def test_sentiment_negative_headline():
    r = sentiment.score_sentiment("어닝쇼크에 급락, 목표가하향")
    assert r.score < -0.3
    assert any(n in r.negatives for n in ("어닝쇼크", "급락", "목표가하향"))


def test_sentiment_neutral_headline():
    r = sentiment.score_sentiment("삼성전자 2분기 실적 발표 예정")
    assert -0.3 <= r.score <= 0.3


def test_news_strategy_bullish_buy():
    d = decide_news(recent_close=70000, sentiment_score=0.6, article_count=5)
    assert d.signal is MACrossSignal.BUY


def test_news_strategy_bearish_sell_when_holding():
    d = decide_news(
        recent_close=70000, sentiment_score=-0.6, article_count=5,
        position_qty=10, avg_price=72000,
    )
    assert d.signal is MACrossSignal.SELL


def test_news_strategy_sparse_holds():
    d = decide_news(recent_close=70000, sentiment_score=0.8, article_count=1)
    assert d.signal is MACrossSignal.HOLD
    assert "sparse" in d.reason


def test_news_strategy_stop_loss():
    d = decide_news(
        recent_close=60000, sentiment_score=0.8, article_count=5,
        position_qty=10, avg_price=70000, stop_loss_pct=5.0,
    )
    assert d.signal is MACrossSignal.SELL
    assert "stop-loss" in d.reason


def test_store_round_trip(tmp_path, monkeypatch):
    # 임시 DB 로 교체
    engine = create_engine(f"sqlite:///{tmp_path}/news.db", future=True)
    monkeypatch.setattr(store, "NEWS_ENGINE", engine)
    store.init_news_db()

    now = datetime.utcnow()
    item = NewsItem(
        symbol="005930",
        title="사상최대 실적",
        url="https://x/1",
        publisher="A",
        published_at=now - timedelta(hours=1),
    )
    assert store.save_news(item, 0.8, "keyword") is True
    # 중복 저장은 False
    assert store.save_news(item, 0.8, "keyword") is False

    score, count, _crit, _sn = store.recent_sentiment("005930", hours=24)
    assert count == 1
    assert score == pytest.approx(0.8)

    # 오래된 기사는 집계에서 제외
    old = NewsItem(
        symbol="005930",
        title="오래된 뉴스",
        url="https://x/2",
        publisher="A",
        published_at=now - timedelta(days=7),
    )
    store.save_news(old, -0.5, "keyword")
    score, count, _crit, _sn = store.recent_sentiment("005930", hours=24)
    assert count == 1  # 여전히 1개만
