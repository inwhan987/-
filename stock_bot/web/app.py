"""FastAPI 웹 대시보드.

라우트:
  GET  /              — 대시보드 (거래·뉴스·포지션·설정)
  GET  /api/trades    — 최근 거래 JSON
  GET  /api/news      — 최근 뉴스 JSON
  GET  /healthz       — 헬스체크

브로커 API 실패해도 페이지는 떠야 하므로 모든 외부 호출은 try/except 로 감싼다.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_KST = timezone(timedelta(hours=9))


def _kst(dt: datetime) -> str:
    """UTC naive datetime → KST 문자열 (DB 저장값이 UTC 기준이므로 +9h)."""
    return dt.replace(tzinfo=timezone.utc).astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from stock_bot.config import settings
from stock_bot.names import get_name
from stock_bot.news.store import NEWS_ENGINE, NewsRow, init_news_db
from stock_bot.storage.db import ENGINE as TRADE_ENGINE
from stock_bot.storage.db import ReviewLog, TradeLog, init_db

STRATEGIES = ("ma_cross", "rsi", "macd", "bollinger", "ensemble", "ema_cross", "momentum", "news", "vwap", "supertrend")
SIZINGS = ("fixed", "fraction", "atr")
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _update_override_key(key: str, value: str | None) -> None:
    """.env.overrides 에서 특정 키를 설정(value) 또는 제거(value=None).

    봇의 env watcher 가 1초 주기로 이 파일을 감시하므로
    재시작 없이 핫리로드된다.
    """
    override_path = ENV_PATH.parent / ".env.overrides"
    lines: list[str] = []
    if override_path.exists():
        for raw in override_path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k == key:
                    continue
            lines.append(raw)
    if value is not None:
        lines.append(f"{key}={value}")
    override_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _update_env_file(updates: dict[str, str]) -> None:
    """`.env` 파일에서 주어진 키들을 업데이트. 없으면 추가. 나머지 줄은 보존.
    `.env.overrides` 에 같은 키가 있으면 제거해 stale override 방지.
    """
    lines: list[str] = []
    seen: set[str] = set()
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    line = f"{key}={updates[key]}"
                    seen.add(key)
            lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # .env 에서 명시한 키는 .env.overrides 에서 제거해 충돌 방지
    override_path = ENV_PATH.parent / ".env.overrides"
    if override_path.exists():
        kept: list[str] = []
        for raw in override_path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    continue  # 제거
            kept.append(raw)
        override_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


class ConfigUpdate(BaseModel):
    strategy: str | None = Field(default=None)
    sizing: str | None = Field(default=None)
    dry_run: bool | None = Field(default=None)
    candle: str | None = Field(default=None)

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _recent_trades(limit: int = 30) -> list[dict]:
    import json as _json
    with Session(TRADE_ENGINE) as s:
        rows = s.scalars(select(TradeLog).order_by(desc(TradeLog.ts)).limit(limit)).all()
        out: list[dict] = []
        for r in rows:
            details = {}
            raw = getattr(r, "details", "") or ""
            if raw:
                try:
                    details = _json.loads(raw)
                except Exception:
                    details = {"raw": raw}
            out.append(
                {
                    "id": r.id,
                    "ts": _kst(r.ts),
                    "symbol": r.symbol,
                    "name": get_name(r.symbol),
                    "side": r.side,
                    "quantity": r.quantity,
                    "price": r.price,
                    "reason": r.reason,
                    "strategy": getattr(r, "strategy", "") or "",
                    "details": details,
                }
            )
        return out


def _recent_reviews(limit: int = 30) -> list[dict]:
    import json as _json
    with Session(TRADE_ENGINE) as s:
        rows = s.scalars(select(ReviewLog).order_by(desc(ReviewLog.ts)).limit(limit)).all()
        out = []
        for r in rows:
            def _dec(x):
                try:
                    return _json.loads(x or "[]")
                except Exception:
                    return []
            out.append({
                "id": r.id,
                "ts": _kst(r.ts),
                "date": r.date,
                "trades_count": r.trades_count,
                "summary": r.summary,
                "findings": _dec(r.findings),
                "suggestions": _dec(r.suggestions),
            })
        return out


def _recent_news(limit: int = 10) -> list[dict]:
    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(select(NewsRow).order_by(desc(NewsRow.published_at)).limit(limit)).all()
        return [
            {
                "symbol": r.symbol,
                "name": get_name(r.symbol),
                "title": r.title,
                "url": r.url,
                "publisher": r.publisher,
                "published_at": r.published_at.strftime("%Y-%m-%d %H:%M"),
                "score": r.sentiment_score,
                "method": r.sentiment_method,
                "is_critical": bool(getattr(r, "is_critical", False)),
                "weight": float(getattr(r, "weight", 1.0)),
            }
            for r in rows
            if r.title and r.title.strip()
        ]


def _news_window_label() -> dict:
    """현재 시점 기준 뉴스 감성 창 정보를 반환.

    Returns:
        {
          "day": "화요일",
          "since_str": "전날 15:30",
          "label": "화요일 · 전날 15:30 ~ 현재",
        }
    """
    from stock_bot.news.store import news_since_kst
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
    now = datetime.now(tz=KST)
    wd = now.weekday()  # 0=월
    day_names = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    day = day_names[wd]

    if now.hour >= 10:
        since_str = "당일 09:00"
    elif wd == 0:
        since_str = "금요일 15:30"
    else:
        since_str = "전날 15:30"

    since_utc = news_since_kst()
    since_kst = since_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(KST)
    since_fmt = since_kst.strftime("%m/%d %H:%M")

    return {
        "day": day,
        "since_str": since_str,
        "since_fmt": since_fmt,
        "label": f"{day} · {since_str} ~ 현재",
    }


def _sentiment_summary() -> tuple[list[dict], dict]:
    from stock_bot.news.store import news_since_kst
    since = news_since_kst()
    window = _news_window_label()
    out: list[dict] = []
    with Session(NEWS_ENGINE) as s:
        for sym in settings.symbols:
            rows = s.scalars(
                select(NewsRow).where(NewsRow.symbol == sym).where(NewsRow.published_at >= since)
            ).all()
            name = get_name(sym)
            if rows:
                total_w = sum(max(getattr(r, "weight", 1.0), 0.01) for r in rows)
                avg = (
                    sum(r.sentiment_score * max(getattr(r, "weight", 1.0), 0.01) for r in rows)
                    / total_w
                )
                crit = sum(1 for r in rows if getattr(r, "is_critical", False))
                out.append(
                    {"symbol": sym, "name": name, "score": avg, "count": len(rows), "critical": crit}
                )
            else:
                out.append({"symbol": sym, "name": name, "score": 0.0, "count": 0, "critical": 0})
    return out, window


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


_ACCOUNT_CACHE: dict = {"at": 0.0, "data": None}
_ACCOUNT_CACHE_TTL = 20.0  # 초. 이 시간 안에 새로고침해도 API 재호출 안 함

_POSITIONS_CACHE: dict = {"at": 0.0, "data": None}
_POSITIONS_CACHE_TTL = 5.0  # 실시간 UI 폴링용 짧은 TTL


def _account_summary(force: bool = False) -> dict:
    """브로커에서 계좌 잔고 요약. 실패 시 0 채워진 dict.

    60초 TTL 메모리 캐시. force=True 면 무시하고 재조회.
    대시보드 새로고침 도중 KIS 쿼터/429 남발 방지.
    """
    blank = {
        "deposit": 0.0,
        "stock_eval": 0.0,
        "total_eval": 0.0,
        "purchase": 0.0,
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "available": False,
        "cached_age": 0,
    }
    now = time.time()
    cached = _ACCOUNT_CACHE["data"]
    age = now - _ACCOUNT_CACHE["at"]
    if cached is not None and not force and age < _ACCOUNT_CACHE_TTL:
        out = dict(cached)
        out["cached_age"] = int(age)
        return out
    try:
        from stock_bot.broker import KISBroker

        broker = KISBroker()
        try:
            s = broker.get_account_summary()
        finally:
            broker.close()
        s["available"] = s.get("total_eval", 0) > 0 or s.get("deposit", 0) > 0
        # 실패/빈 응답(available=False) 은 캐시 오염 방지 위해 저장 안 함
        if s["available"]:
            _ACCOUNT_CACHE["data"] = s
            _ACCOUNT_CACHE["at"] = now
            out = dict(s)
            out["cached_age"] = 0
            return out
        # 이번 조회는 실패 — 이전 유효 캐시가 있으면 그것 사용
        if cached is not None:
            out = dict(cached)
            out["cached_age"] = int(age)
            return out
        return blank
    except Exception as exc:
        logger.info("account summary fetch failed (likely no credentials): {}", exc)
        # 조회 실패해도 직전 캐시가 있으면 그대로 반환
        if cached is not None:
            out = dict(cached)
            out["cached_age"] = int(age)
            return out
        return blank


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
        sentiment, news_window = _sentiment_summary()
        positions = _live_positions()
        account = _account_summary()
        cfg = {
            "strategy": settings.trade_strategy,
            "sizing": settings.position_sizing,
            "dry_run": settings.trade_dry_run,
            "env": settings.kis_env,
            "symbols": settings.symbols,
            "symbol_names": {s: get_name(s) for s in settings.symbols},
            "candle": settings.live_candle,
            "interval": settings.live_interval_minutes,
            "news_enabled": settings.news_enabled,
        }
        resp = templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "trades": trades,
                "news": news,
                "sentiment": sentiment,
                "news_window": news_window,
                "positions": positions,
                "account": account,
                "config": cfg,
            },
        )
        # 개발 중에는 브라우저 캐시 비활성 — 템플릿 수정 즉시 반영되도록.
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    @app.get("/reasons", response_class=HTMLResponse)
    def reasons(request: Request):
        trades = _recent_trades(limit=100)
        reviews = _recent_reviews(limit=30)
        resp = templates.TemplateResponse(
            request,
            "reasons.html",
            {"trades": trades, "reviews": reviews, "config": {"strategy": settings.trade_strategy}},
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    @app.get("/api/reasons")
    def api_reasons(limit: int = 100):
        return JSONResponse(_recent_trades(limit))

    @app.get("/api/reviews")
    def api_reviews(limit: int = 30):
        return JSONResponse(_recent_reviews(limit))

    @app.get("/api/trades")
    def api_trades(limit: int = 30):
        return JSONResponse(_recent_trades(limit))

    @app.get("/api/news")
    def api_news(limit: int = 10):
        return JSONResponse(_recent_news(limit))

    @app.get("/api/positions")
    def api_positions():
        """실시간 포지션 조회 (5초 TTL 캐시). 대시보드 실시간 가격 폴링용."""
        now = time.time()
        cached = _POSITIONS_CACHE["data"]
        age = now - _POSITIONS_CACHE["at"]
        if cached is not None and age < _POSITIONS_CACHE_TTL:
            return JSONResponse(cached)
        data = _live_positions()
        _POSITIONS_CACHE["data"] = data
        _POSITIONS_CACHE["at"] = now
        return JSONResponse(data)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/api/account/refresh")
    def api_account_refresh():
        """캐시 무시하고 KIS 에서 잔고 재조회."""
        return JSONResponse(_account_summary(force=True))

    @app.post("/api/config")
    def update_config(payload: ConfigUpdate):
        updates: dict[str, str] = {}
        if payload.strategy is not None:
            if payload.strategy not in STRATEGIES:
                raise HTTPException(400, f"invalid strategy: {payload.strategy}")
            settings.trade_strategy = payload.strategy  # type: ignore[assignment]
            updates["TRADE_STRATEGY"] = payload.strategy
        if payload.sizing is not None:
            if payload.sizing not in SIZINGS:
                raise HTTPException(400, f"invalid sizing: {payload.sizing}")
            settings.position_sizing = payload.sizing  # type: ignore[assignment]
            updates["POSITION_SIZING"] = payload.sizing
        if payload.dry_run is not None:
            settings.trade_dry_run = payload.dry_run
            _update_override_key(
                "TRADE_DRY_RUN",
                "false" if not payload.dry_run else "true",
            )
            logger.info("dry_run 변경: {} → .env.overrides 반영 (봇 핫리로드)", payload.dry_run)
        if payload.candle is not None:
            if payload.candle not in ("daily", "minute"):
                raise HTTPException(400, f"invalid candle: {payload.candle}")
            settings.live_candle = payload.candle  # type: ignore[assignment]
            _update_override_key("LIVE_CANDLE", payload.candle)
            logger.info("candle 변경: {} → .env.overrides 반영 (봇 핫리로드)", payload.candle)
        if not updates:
            if payload.dry_run is None:
                raise HTTPException(400, "no fields to update")
        else:
            _update_env_file(updates)
            logger.info("config updated via UI: {}", updates)
        return {
            "ok": True,
            "strategy": settings.trade_strategy,
            "sizing": settings.position_sizing,
            "dry_run": settings.trade_dry_run,
        }

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
