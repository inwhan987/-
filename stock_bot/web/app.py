"""FastAPI 웹 대시보드.

라우트:
  GET  /              — 대시보드 (거래·뉴스·포지션·설정)
  GET  /api/trades    — 최근 거래 JSON
  GET  /api/news      — 최근 뉴스 JSON
  GET  /healthz       — 헬스체크

브로커 API 실패해도 페이지는 떠야 하므로 모든 외부 호출은 try/except 로 감싼다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from stock_bot.config import settings
from stock_bot.news.store import NEWS_ENGINE, NewsRow, init_news_db
from stock_bot.storage.db import ENGINE as TRADE_ENGINE
from stock_bot.storage.db import TradeLog, init_db

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _recent_trades(limit: int = 30) -> list[dict]:
    with Session(TRADE_ENGINE) as s:
        rows = s.scalars(select(TradeLog).order_by(desc(TradeLog.ts)).limit(limit)).all()
        return [
            {
                "id": r.id,
                "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": r.symbol,
                "side": r.side,
                "quantity": r.quantity,
                "price": r.price,
                "reason": r.reason,
            }
            for r in rows
        ]


def _recent_news(limit: int = 30) -> list[dict]:
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(select(NewsRow).order_by(desc(NewsRow.published_at)).limit(limit)).all()
        return [
            {
                "symbol": r.symbol,
                "title": r.title,
                "url": r.url,
                "publisher": r.publisher,
                "published_at": r.published_at.strftime("%Y-%m-%d %H:%M"),
                "score": r.sentiment_score,
                "method": r.sentiment_method,
            }
            for r in rows
        ]


def _sentiment_summary(hours: int = 24) -> list[dict]:
    since = datetime.utcnow() - timedelta(hours=hours)
    out: list[dict] = []
    with Session(NEWS_ENGINE) as s:
        for sym in settings.symbols:
            rows = s.scalars(
                select(NewsRow).where(NewsRow.symbol == sym).where(NewsRow.published_at >= since)
            ).all()
            if rows:
                avg = sum(r.sentiment_score for r in rows) / len(rows)
                out.append({"symbol": sym, "score": avg, "count": len(rows)})
            else:
                out.append({"symbol": sym, "score": 0.0, "count": 0})
    return out


def _live_positions() -> list[dict]:
    """브로커에서 현재 잔고 조회. 실패하면 빈 리스트."""
    try:
        from stock_bot.broker import KISBroker

        broker = KISBroker()
        try:
            rows = broker.get_positions()
        finally:
            broker.close()
        return [
            {
                "symbol": r.get("pdno", ""),
                "name": r.get("prdt_name", ""),
                "qty": int(r.get("hldg_qty", 0) or 0),
                "avg": float(r.get("pchs_avg_pric", 0) or 0),
                "current": float(r.get("prpr", 0) or 0),
                "pl_pct": float(r.get("evlu_pfls_rt", 0) or 0),
            }
            for r in rows
            if int(r.get("hldg_qty", 0) or 0) > 0
        ]
    except Exception as exc:
        logger.info("positions fetch failed (likely no credentials): {}", exc)
        return []


def create_app() -> FastAPI:
    init_db()
    init_news_db()
    app = FastAPI(title="stock-bot dashboard")
    static_dir = BASE / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        trades = _recent_trades()
        news = _recent_news()
        sentiment = _sentiment_summary()
        positions = _live_positions()
        cfg = {
            "strategy": settings.trade_strategy,
            "sizing": settings.position_sizing,
            "dry_run": settings.trade_dry_run,
            "env": settings.kis_env,
            "symbols": settings.symbols,
            "candle": settings.live_candle,
            "interval": settings.live_interval_minutes,
            "news_enabled": settings.news_enabled,
        }
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "trades": trades,
                "news": news,
                "sentiment": sentiment,
                "positions": positions,
                "config": cfg,
            },
        )

    @app.get("/api/trades")
    def api_trades(limit: int = 30):
        return JSONResponse(_recent_trades(limit))

    @app.get("/api/news")
    def api_news(limit: int = 30):
        return JSONResponse(_recent_news(limit))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


def run_web() -> None:
    import uvicorn

    uvicorn.run(
        "stock_bot.web.app:create_app",
        host=settings.web_host,
        port=settings.web_port,
        factory=True,
        reload=False,
    )
