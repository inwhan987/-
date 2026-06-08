"""FastAPI 웹 대시보드.

라우트:
  GET  /              — 대시보드 (거래·뉴스·포지션·설정)
  GET  /api/trades    — 최근 거래 JSON
  GET  /api/news      — 최근 뉴스 JSON
  GET  /healthz       — 헬스체크

브로커 API 실패해도 페이지는 떠야 하므로 모든 외부 호출은 try/except 로 감싼다.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger


class _InterceptHandler(logging.Handler):
    """uvicorn/Python 표준 logging → loguru 중계 핸들러."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _setup_uvicorn_log_intercept() -> None:
    """uvicorn / fastapi 로그만 loguru 로 중계 (→ stock_web.log 로 기록).

    루트 로거는 건드리지 않아 sqlalchemy·httpx 등 봇 공용 모듈 로그가
    웹 로그에 섞이지 않도록 한다.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        log = logging.getLogger(name)
        log.handlers = [_InterceptHandler()]
        log.propagate = False
        log.setLevel(logging.INFO)

from stock_bot.market_calendar import KST as _KST


def _kst(dt: datetime) -> str:
    """UTC naive datetime → KST 문자열 (DB 저장값이 UTC 기준이므로 +9h)."""
    return dt.replace(tzinfo=timezone.utc).astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from stock_bot.config import settings
from stock_bot.names import get_name
from stock_bot.notify import notify
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
    initial_capital: float | None = Field(default=None)
    fee_buy_pct: float | None = Field(default=None)
    fee_sell_pct: float | None = Field(default=None)


class BacktestRequest(BaseModel):
    symbol: str = "005930.KS"
    period: str = "60d"


class ScreenerRequest(BaseModel):
    sector: str = "IT"
    top_n: int = 3
    market_top: int = 200


class ParamUpdate(BaseModel):
    updates: dict[str, str]


class HolidayUpdate(BaseModel):
    dates: list[str]

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _recent_trades(limit: int = 30) -> list[dict]:
    import json as _json
    with Session(TRADE_ENGINE) as s:
        rows = s.scalars(select(TradeLog).order_by(desc(TradeLog.ts)).limit(limit * 3)).all()
        out: list[dict] = []
        for r in rows:
            try:
                broker_resp = _json.loads(r.broker_response) if r.broker_response else {}
            except Exception:
                broker_resp = {}
            if broker_resp.get("dry_run"):
                continue
            details = {}
            raw = getattr(r, "details", "") or ""
            if raw:
                try:
                    details = _json.loads(raw)
                except Exception:
                    details = {"raw": raw}
            avg_price = details.get("avg_price", 0.0) or 0.0
            pnl_pct = (
                (r.price - avg_price) / avg_price * 100
                if r.side == "sell" and avg_price > 0
                else None
            )
            out.append(
                {
                    "id": r.id,
                    "ts": _kst(r.ts),
                    "symbol": r.symbol,
                    "name": get_name(r.symbol),
                    "side": r.side,
                    "quantity": r.quantity,
                    "price": r.price,
                    "avg_price": avg_price,
                    "pnl_pct": pnl_pct,
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
    import re as _re

    def _title_key(t: str) -> str:
        """대괄호 태그·공백 제거 후 앞 30자로 중복 판정."""
        t = _re.sub(r"^\[[^\]]+\]\s*", "", t or "")
        t = _re.sub(r"[\s·ㆍ]+", "", t)
        return t[:30]

    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow).order_by(desc(NewsRow.published_at)).limit(limit * 4)
        ).all()
        seen: set[str] = set()
        result: list[dict] = []
        for r in rows:
            if not r.title or not r.title.strip():
                continue
            key = _title_key(r.title)
            if key in seen:
                continue
            seen.add(key)
            result.append({
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
            })
            if len(result) >= limit:
                break
        return result


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

    now = datetime.now(tz=_KST)
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
    since_kst = since_utc.replace(tzinfo=timezone.utc).astimezone(_KST)
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
            code = sym.split(".")[0]  # 005930.KS → 005930
            rows = s.scalars(
                select(NewsRow).where(NewsRow.symbol == code).where(NewsRow.published_at >= since)
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
                    {"symbol": code, "name": name, "score": avg, "count": len(rows), "critical": crit}
                )
            else:
                out.append({"symbol": code, "name": name, "score": 0.0, "count": 0, "critical": 0})
    return out, window


def _realized_pnl_summary() -> dict:
    """TradeLog 전체에서 실현손익·거래횟수 계산 (FIFO 매칭)."""
    from collections import deque
    from datetime import datetime as _dt
    import json as _json2
    def _is_dry(r) -> bool:
        try:
            return bool(_json2.loads(r.broker_response or "{}").get("dry_run"))
        except Exception:
            return False
    with Session(TRADE_ENGINE) as s:
        rows = [r for r in s.scalars(select(TradeLog).order_by(TradeLog.ts)).all() if not _is_dry(r)]

    start_dt = None
    if settings.perf_start_date:
        try:
            from datetime import timedelta as _td
            # PERF_START_DATE는 KST 기준 — DB는 UTC 저장이므로 9시간 빼서 비교
            kst_midnight = _dt.strptime(settings.perf_start_date, "%Y-%m-%d")
            start_dt = kst_midnight - _td(hours=9)
        except ValueError:
            pass

    total_realized = 0.0
    buy_count = 0
    sell_count = 0
    # 종목별 매수 큐: deque of [price, remaining_qty]
    buy_queues: dict[str, deque] = {}

    for r in rows:
        if start_dt and r.ts < start_dt:
            continue
        sym = r.symbol
        if sym not in buy_queues:
            buy_queues[sym] = deque()
        if r.side == "buy":
            buy_count += 1
            buy_queues[sym].append([r.price, r.quantity])
        elif r.side == "sell":
            sell_count += 1
            remaining = r.quantity
            while remaining > 0 and buy_queues.get(sym):
                buy_price, buy_qty = buy_queues[sym][0]
                matched = min(remaining, buy_qty)
                gross = (r.price - buy_price) * matched
                # 모의투자: 증권거래세(0.18%) 미부과 → 수수료(0.015%)만 차감
                effective_sell_fee = settings.trade_fee_buy_pct if settings.is_paper else settings.trade_fee_sell_pct
                fee = (r.price * effective_sell_fee
                       + buy_price * settings.trade_fee_buy_pct) * matched
                total_realized += gross - fee
                remaining -= matched
                buy_queues[sym][0][1] -= matched
                if buy_queues[sym][0][1] <= 0:
                    buy_queues[sym].popleft()

    initial = settings.initial_capital_krw
    pnl_pct = (total_realized / initial * 100) if initial > 0 else None
    return {
        "realized_pnl": total_realized,
        "pnl_pct": pnl_pct,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_trades": buy_count + sell_count,
        "initial_capital": initial,
        "fee_buy_pct": settings.trade_fee_buy_pct * 100,
        "fee_sell_pct": (settings.trade_fee_buy_pct if settings.is_paper else settings.trade_fee_sell_pct) * 100,
        "is_paper": settings.is_paper,
    }


def _merge_positions_into_symbols(symbols_str: str) -> str:
    """스크리너/수동 저장 시 열린 포지션 종목이 SYMBOLS에서 빠지지 않도록 병합."""
    try:
        pos_syms = [p["symbol"] for p in _live_positions() if p.get("symbol")]
        if not pos_syms:
            return symbols_str
        sym_list = [s for s in symbols_str.split(",") if s.strip()]
        # 6자리 코드만 추출해서 비교 (005930.KS → 005930)
        existing_codes = {s.split(".")[0] for s in sym_list}
        added = []
        for ps in pos_syms:
            code = ps.split(".")[0]  # suffix 제거 → 6자리 통일
            if code not in existing_codes:
                sym_list.append(code)
                existing_codes.add(code)
                added.append(code)
        if added:
            logger.info("포지션 보유 종목 SYMBOLS에 유지: {}", added)
        return ",".join(sym_list)
    except Exception:
        return symbols_str


def _live_positions() -> list[dict]:
    """브로커에서 현재 잔고 조회. 실패하면 빈 리스트."""
    global _broker_instance
    try:
        broker = _get_broker()
        if broker is None:
            return []
        rows = broker.get_positions()
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
        _broker_instance = None  # 에러 시 다음 호출에서 재생성
        return []


_ACCOUNT_CACHE: dict = {"at": 0.0, "data": None}
_ACCOUNT_CACHE_TTL = 25.0  # 초. 30초 폴링 주기보다 짧게 설정

_POSITIONS_CACHE: dict = {"at": 0.0, "data": None}
_POSITIONS_CACHE_TTL = 5.0  # 실시간 UI 폴링용 짧은 TTL

_broker_instance = None

def _get_broker():
    """KISBroker 싱글턴 반환. httpx.Client 를 재사용해 fd 누수 방지."""
    global _broker_instance
    if _broker_instance is None:
        try:
            from stock_bot.broker import KISBroker
            _broker_instance = KISBroker()
        except Exception:
            return None
    return _broker_instance


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
        broker = _get_broker()
        if broker is None:
            return {**blank, "cached_age": 0}
        s = broker.get_account_summary()
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
        _broker_instance = None  # 에러 시 다음 호출에서 재생성
        if cached is not None:
            out = dict(cached)
            out["cached_age"] = int(age)
            return out
        return blank


def create_app() -> FastAPI:
    _setup_uvicorn_log_intercept()
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
        perf = _realized_pnl_summary()
        # 브로커 기반 총 손익 (total_eval - initial_capital) — 가장 정확한 숫자
        initial = settings.initial_capital_krw
        if account.get("available") and account.get("total_eval", 0) > 0 and initial > 0:
            net_pnl = account["total_eval"] - initial
            perf["net_pnl"] = net_pnl
            perf["net_pnl_pct"] = net_pnl / initial * 100
            perf["net_pnl_available"] = True
        else:
            perf["net_pnl"] = 0.0
            perf["net_pnl_pct"] = 0.0
            perf["net_pnl_available"] = False
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
                "perf": perf,
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

    @app.get("/api/sentiment")
    def api_sentiment():
        sentiment, window = _sentiment_summary()
        return JSONResponse({"sentiment": sentiment, "news_window": window})

    @app.get("/api/positions")
    def api_positions(force: bool = False):
        """실시간 포지션 조회 (5초 TTL 캐시). force=true 면 캐시 무시.

        주의: 빈 리스트는 캐시 안 함 → KIS 일시 500 에러 시 직전 정상 캐시 유지
        (이전 버그: 빈 리스트 캐시 → 5초 동안 매수해도 포지션 안 보임)
        """
        now = time.time()
        cached = _POSITIONS_CACHE["data"]
        age = now - _POSITIONS_CACHE["at"]
        if not force and cached and age < _POSITIONS_CACHE_TTL:
            return JSONResponse(cached)
        data = _live_positions()
        # 빈 리스트는 캐시 안 함 (KIS 일시 에러 대비)
        if data:
            _POSITIONS_CACHE["data"] = data
            _POSITIONS_CACHE["at"] = now
            return JSONResponse(data)
        # 이번 조회 실패 — 직전 정상 캐시 있으면 반환
        if cached:
            return JSONResponse(cached)
        return JSONResponse([])

    @app.post("/api/positions/refresh")
    def api_positions_refresh():
        """캐시 무시하고 KIS 에서 포지션 강제 재조회."""
        data = _live_positions()
        if data:
            _POSITIONS_CACHE["data"] = data
            _POSITIONS_CACHE["at"] = time.time()
        return JSONResponse(data)

    @app.get("/params", response_class=HTMLResponse)
    def params_page(request: Request):
        template_path = Path(__file__).parent / "templates" / "params.html"
        resp = HTMLResponse(template_path.read_text(encoding="utf-8"))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    @app.get("/api/overrides/raw")
    def api_overrides_raw():
        """.env.overrides 파일 원본 텍스트 반환 — PC 동기화용."""
        from fastapi.responses import PlainTextResponse
        override_path = ENV_PATH.parent / ".env.overrides"
        if not override_path.exists():
            return PlainTextResponse("", status_code=200)
        return PlainTextResponse(override_path.read_text(encoding="utf-8"))

    @app.get("/api/params")
    def api_get_params():
        """.env → .env.overrides 순서로 읽어 파라미터 반환 (overrides 우선)."""
        def _read_env_file(path: Path) -> dict[str, str]:
            out: dict[str, str] = {}
            if not path.exists():
                return out
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.split("#")[0].strip()
            return out

        result = _read_env_file(ENV_PATH)                          # .env 기본값
        result.update(_read_env_file(ENV_PATH.parent / ".env.overrides"))  # overrides 우선
        return JSONResponse(result)

    ALLOWED_PARAM_KEYS = {
        "ENSEMBLE_WEIGHTS", "ENSEMBLE_BUY_THRESHOLD", "ENSEMBLE_SELL_THRESHOLD",
        "ENSEMBLE_MIN_BUY_VOTES", "ENSEMBLE_MIN_SELL_VOTES",
        "TRADE_VWAP_BAND", "TRADE_VWAP_SELL_BAND", "TRADE_VWAP_ST_BULL_SELL_BAND", "TRADE_VWAP_WARMUP_BARS",
        "TRADE_RSI_PERIOD", "TRADE_RSI_OVERSOLD", "TRADE_RSI_OVERBOUGHT",
        "TRADE_SUPERTREND_PERIOD", "TRADE_SUPERTREND_MULT",
        "TRADE_BB_WINDOW", "TRADE_BB_K", "TRADE_BB_CONSEC",
        "ADD_BUY_ENABLED", "ADD_BUY_THRESHOLD", "ADD_BUY_MIN_VOTES",
        "ADD_BUY_MAX_COUNT", "ADD_BUY_FRACTION", "ADD_BUY_MAX_POSITION_PCT",
        "ADD_BUY_REQUIRE_TREND_AGREE", "ADD_BUY_INHERIT_INITIAL_STOP",
        "POST_STOPLOSS_COOLDOWN_MIN",
        "ENGINE_HARD_STOP_ENABLED", "ENGINE_HARD_STOP_PCT", "DAILY_MAX_LOSS_PCT",
        "ATR_STOP_LOSS_ENABLED", "ATR_PERIOD", "ATR_STOP_MULTIPLIER", "ATR_STOP_MAX_PCT",
        "ENSEMBLE_VOLUME_FILTER_ENABLED", "ENSEMBLE_VOLUME_MA_PERIOD",
        "ENSEMBLE_VOLUME_HIGH_RATIO", "ENSEMBLE_VOLUME_LOW_RATIO",
        "ENSEMBLE_VOLUME_SCORE_BOOST", "ENSEMBLE_VOLUME_SCORE_PENALTY",
        "ENTRY_BLOCK_ENABLED", "ENTRY_BLOCK_START", "ENTRY_BLOCK_END",
        "ENTRY_BLOCK_MIN_PROFIT_TO_SELL_PCT", "ENTRY_BLOCK_FORCE_SELL_FRACTION",
        "CLOSE_BLOCK_ENABLED", "CLOSE_BLOCK_START",
        "TAKE_PROFIT_ENABLED", "TAKE_PROFIT_PCT", "TAKE_PROFIT_FRACTION",
        "POSITION_SIZING", "POSITION_FRACTION",
        "DAILY_CONTEXT_PROFIT_GATE_PCT", "DAILY_CONTEXT_AVWAP_PCT",
        "DAILY_CONTEXT_PDH_PCT", "DAILY_CONTEXT_PDC_PCT",
        "DAILY_CONTEXT_TREND_BONUS",
        "OVERNIGHT_SELL_THRESHOLD", "OVERNIGHT_MIN_SELL_VOTES",
        "NEWS_PREFER_LLM", "NEWS_PAGES_PER_SYMBOL",
        "ENSEMBLE_NEWS_VETO_THRESHOLD", "ENSEMBLE_NEWS_STRONG_NEG_RATIO",
        "SELL_ON_NEXT_OPEN",
        "SYMBOLS",
        "SCREENER_SECTOR", "SCREENER_TOP_N",
        "SCREENER_AUTO_SECTOR", "SCREENER_DOWNTREND_HALVE",
        "SCREENER_RS_DAYS", "SCREENER_MIN_STOCKS",
        "TRADE_DRY_RUN",
    }

    @app.post("/api/params")
    def api_save_params(body: ParamUpdate):
        """변경된 파라미터를 .env.overrides 에 저장 (로컬만, git 커밋 안 함).

        git 반영은 PC에서 Claude 통해 커밋+푸시 → update.sh 로 처리.
        """
        import re as _re
        override_path = ENV_PATH.parent / ".env.overrides"
        safe = {k: v for k, v in body.updates.items() if k in ALLOWED_PARAM_KEYS}
        if not safe:
            return JSONResponse({"ok": False, "error": "no allowed keys"})
        # B안: SYMBOLS 수동 저장 시에도 포지션 종목 병합
        if "SYMBOLS" in safe and safe["SYMBOLS"]:
            safe["SYMBOLS"] = _merge_positions_into_symbols(safe["SYMBOLS"])
        text = override_path.read_text(encoding="utf-8") if override_path.exists() else ""
        for key, val in safe.items():
            pattern = rf"^({_re.escape(key)}\s*=).*$"
            new_text, n = _re.subn(pattern, rf"\g<1>{val}", text, flags=_re.MULTILINE)
            text = new_text if n > 0 else text.rstrip() + f"\n{key}={val}\n"
        override_path.write_text(text, encoding="utf-8")
        if "TRADE_DRY_RUN" in safe:
            settings.trade_dry_run = safe["TRADE_DRY_RUN"].lower() == "true"
        if "SYMBOLS" in safe:
            settings.trade_symbols = safe["SYMBOLS"]
        logger.info("파라미터 웹 UI 저장 (로컬): {}", list(safe.keys()))

        return JSONResponse({"ok": True, "saved": list(safe.keys())})

    @app.get("/api/holidays")
    def api_get_holidays():
        """추가 휴장일 조회. user=수동입력(편집가능)."""
        from stock_bot.market_calendar import load_user_holidays
        return JSONResponse({
            "user": sorted(load_user_holidays()),
        })

    @app.post("/api/holidays")
    def api_save_holidays(body: HolidayUpdate):
        """수동 휴장일 전체 목록을 저장(재시작 없이 봇·웹에 반영)."""
        from stock_bot.market_calendar import save_user_holidays
        try:
            saved = save_user_holidays(body.dates)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        logger.info("수동 휴장일 저장: {}", saved)
        return JSONResponse({"ok": True, "user": saved})

    # ── 백테스트 job 저장소 (메모리 + JSON 파일 영속화) ───────────────────────
    _BT_JOBS: dict[str, dict] = {}  # job_id → {status, output, started_at, ...}
    _BT_HISTORY_PATH = Path("/app/data/backtest_history.json") if Path("/app/data").exists() else (Path(__file__).resolve().parents[2] / "data" / "backtest_history.json")
    _BT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)

    def _load_bt_history() -> list[dict]:
        """디스크에서 백테스트 히스토리 로드 (최신 100건만 유지)."""
        if not _BT_HISTORY_PATH.exists():
            return []
        try:
            import json as _json
            return _json.loads(_BT_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_bt_history(job_id: str, job: dict) -> None:
        """완료된 job 을 디스크에 영속 저장 (최신 100건)."""
        try:
            import json as _json
            history = _load_bt_history()
            entry = {
                "job_id": job_id,
                "status": job.get("status", ""),
                "output": job.get("output", ""),
                "symbol": job.get("symbol", ""),
                "period": job.get("period", ""),
                "script": job.get("script", "backtest_current.py"),
                "started_at": job.get("started_at", 0),
                "finished_at": _time.time(),
            }
            history.insert(0, entry)
            history = history[:100]
            _BT_HISTORY_PATH.write_text(_json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("backtest history 저장 실패: {}", e)

    # 시작 시 히스토리 로드 → _BT_JOBS 복원
    import time as _time
    for _entry in _load_bt_history():
        _jid = _entry.get("job_id")
        if _jid:
            _BT_JOBS[_jid] = {
                "status": _entry.get("status", "done"),
                "output": _entry.get("output", ""),
                "symbol": _entry.get("symbol", ""),
                "period": _entry.get("period", ""),
                "script": _entry.get("script", "backtest_current.py"),
                "started_at": _entry.get("started_at", 0),
            }

    def _run_bt_job(job_id: str, symbols: str, period: str, script: str = "backtest_current.py") -> None:
        """별도 스레드에서 백테스트 스크립트 실행 후 결과를 _BT_JOBS + 디스크에 저장."""
        import subprocess as _sp
        root = Path(__file__).resolve().parents[2]
        bt_script = root / script
        try:
            result = _sp.run(
                [sys.executable, str(bt_script), symbols, period],
                capture_output=True, text=True, timeout=900,
                cwd=str(root),
            )
            output = result.stdout or result.stderr or "(출력 없음)"
            _BT_JOBS[job_id].update({"status": "done", "output": output})
        except _sp.TimeoutExpired:
            _BT_JOBS[job_id].update({"status": "error", "output": "타임아웃 (900초 초과)"})
        except Exception as e:
            _BT_JOBS[job_id].update({"status": "error", "output": str(e)})
        # 디스크 영속화
        _save_bt_history(job_id, _BT_JOBS[job_id])

    @app.post("/api/backtest")
    def api_backtest(req: BacktestRequest):
        """백테스트를 백그라운드 스레드로 시작하고 job_id 반환."""
        import time
        from stock_bot.names import resolve_symbol
        symbols = ",".join(resolve_symbol(s) for s in req.symbol.split(","))
        job_id = uuid.uuid4().hex
        _BT_JOBS[job_id] = {"status": "running", "output": "", "symbol": symbols, "period": req.period, "script": "backtest_current.py", "started_at": time.time()}
        t = threading.Thread(target=_run_bt_job, args=(job_id, symbols, req.period), daemon=True)
        t.start()
        return JSONResponse({"ok": True, "job_id": job_id})

    @app.post("/api/backtest/compare")
    def api_backtest_compare(req: BacktestRequest):
        """필터 비교 백테스트 (backtest_compare.py) — 실전 러너 동일 설정."""
        import time
        from stock_bot.names import resolve_symbol
        symbols = ",".join(resolve_symbol(s) for s in req.symbol.split(","))
        job_id = uuid.uuid4().hex
        _BT_JOBS[job_id] = {"status": "running", "output": "", "symbol": symbols, "period": req.period, "script": "backtest_compare.py", "started_at": time.time()}
        t = threading.Thread(
            target=_run_bt_job,
            args=(job_id, symbols, req.period, "backtest_compare.py"),
            daemon=True,
        )
        t.start()
        return JSONResponse({"ok": True, "job_id": job_id})

    @app.get("/api/backtest/history")
    def api_backtest_history(limit: int = 20):
        """저장된 백테스트 히스토리 (최신순, 최대 limit건)."""
        history = _load_bt_history()[:limit]
        # output 은 미리보기용으로 짧게
        return JSONResponse([{
            "job_id": e.get("job_id", ""),
            "symbol": e.get("symbol", ""),
            "period": e.get("period", ""),
            "script": e.get("script", "backtest_current.py"),
            "status": e.get("status", ""),
            "started_at": e.get("started_at", 0),
            "finished_at": e.get("finished_at", 0),
            "output_preview": (e.get("output", "")[:300] + "...") if len(e.get("output", "")) > 300 else e.get("output", ""),
        } for e in history])

    @app.get("/api/backtest/latest")
    def api_backtest_latest():
        """가장 최근 백테스트 job 반환 — 기기 전환 시 자동 재연결용."""
        import time
        if not _BT_JOBS:
            return JSONResponse({"job_id": None})
        # started_at 기준 최신 job 선택
        latest_id = max(_BT_JOBS, key=lambda jid: _BT_JOBS[jid].get("started_at", 0))
        job = _BT_JOBS[latest_id]
        # 완료 후 1시간 지난 job은 반환 안 함
        age = time.time() - job.get("started_at", 0)
        if job["status"] != "running" and age > 3600:
            return JSONResponse({"job_id": None})
        return JSONResponse({
            "job_id": latest_id,
            "status": job["status"],
            "output": job["output"],
            "symbol": job.get("symbol", ""),
            "period": job.get("period", ""),
        })

    @app.get("/api/backtest/{job_id}")
    def api_backtest_status(job_id: str):
        """백테스트 job 상태/결과 조회."""
        job = _BT_JOBS.get(job_id)
        if job is None:
            return JSONResponse({"status": "not_found", "output": ""})
        return JSONResponse({"status": job["status"], "output": job["output"]})

    # ── 대장주 선별 job (leader_finder.py) ─────────────────────────────────────
    # 업종/테마 기반 대장주를 즉시 1회 선별. 결과 stdout은 웹에 표시되고,
    # 디스코드 알림은 leader_finder.py 가 직접 발송한다(부모 환경의 WEBHOOK 상속).
    _LD_JOBS: dict[str, dict] = {}

    def _run_ld_job(job_id: str, mode: str) -> None:
        import subprocess as _sp
        root = Path(__file__).resolve().parents[2]
        script = root / "leader_finder.py"
        cmd = [sys.executable, str(script), "--once", "--ignore-hours", "--summary-only"]
        if mode == "theme":
            cmd.append("--theme")
        try:
            result = _sp.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300, cwd=str(root),
            )
            output = result.stdout or result.stderr or "(출력 없음)"
            _LD_JOBS[job_id].update({"status": "done", "output": output})
        except _sp.TimeoutExpired:
            _LD_JOBS[job_id].update({"status": "error", "output": "타임아웃 (300초 초과)"})
        except Exception as e:
            _LD_JOBS[job_id].update({"status": "error", "output": str(e)})

    @app.post("/api/leader/run")
    def api_leader_run(mode: str = "sector"):
        """대장주 선별 즉시 실행. mode=sector(업종)|theme(테마). 결과는 디스코드로도 발송."""
        m = "theme" if mode == "theme" else "sector"
        job_id = uuid.uuid4().hex
        _LD_JOBS[job_id] = {"status": "running", "output": "", "mode": m, "started_at": time.time()}
        t = threading.Thread(target=_run_ld_job, args=(job_id, m), daemon=True)
        t.start()
        return JSONResponse({"ok": True, "job_id": job_id, "mode": m})

    @app.get("/api/leader/{job_id}")
    def api_leader_status(job_id: str):
        """대장주 선별 job 상태/결과 조회."""
        job = _LD_JOBS.get(job_id)
        if job is None:
            return JSONResponse({"status": "not_found", "output": ""})
        return JSONResponse({"status": job["status"], "output": job["output"], "mode": job.get("mode", "")})

    # ── 스크리너 job 저장소 ────────────────────────────────────────────────────
    _SC_JOBS: dict[str, dict] = {}

    def _read_screener_cfg() -> dict:
        """현재 스크리너 설정값(.env.overrides 우선) 반환."""
        def _read_kv(path: Path) -> dict:
            out: dict[str, str] = {}
            if path.exists():
                for ln in path.read_text(encoding="utf-8").splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith("#") and "=" in ln:
                        k, _, v = ln.partition("=")
                        out[k.strip()] = v.split("#")[0].strip()
            return out
        env = _read_kv(ENV_PATH)
        env.update(_read_kv(ENV_PATH.parent / ".env.overrides"))
        def _b(v: str) -> bool:
            return str(v).strip().lower() in ("1", "true", "y", "yes", "on")
        return {
            "sector":     env.get("SCREENER_SECTOR",     ""),
            "top_n":      int(env.get("SCREENER_TOP_N",      "6")),
            "market_top": int(env.get("SCREENER_MARKET_TOP", "100")),
            # ── 장전 자동 분석(장분석+섹터분석) ──────────────────────────
            "auto_sector":     _b(env.get("SCREENER_AUTO_SECTOR", "1")),   # 기본 ON
            "rs_days":         int(env.get("SCREENER_RS_DAYS",    "20")),
            "min_stocks":      int(env.get("SCREENER_MIN_STOCKS", "5")),
            "universe_top":    int(env.get("SCREENER_UNIVERSE_TOP", "200")),
            "downtrend_halve": _b(env.get("SCREENER_DOWNTREND_HALVE", "1")),
        }

    _SC_LOG_PATH = (
        Path("/app/data/screener_latest.log")
        if Path("/app/data").exists()
        else Path(__file__).resolve().parents[2] / "data" / "screener_latest.log"
    )

    _SC_LOCK = threading.Lock()  # 스크리너 중복 실행 방지용 락

    # SSE 용 in-memory 스트림 버퍼 — 파일 I/O 의존 없음, append-only (cursor 기반 tail)
    _SC_STREAM_BUF: list[str] = []
    _SC_STREAM_MAX = 5000  # 최대 보관 줄 수

    def _run_sc_job(job_id: str, sector: str, top_n: int, market_top: int,
                    auto: bool = False) -> None:
        """별도 스레드에서 screener.py 실행 → 결과 파싱 → SYMBOLS 자동 업데이트.

        출력을 파일로 스트리밍 → 메모리 버퍼 없음, docker logs 실시간 표시.
        auto=True(자동 트리거)면 screener 실행 전 장분석+섹터분석으로
        섹터/종목수/시장범위를 자동 결정한다.
        """
        import re as _re
        import subprocess as _sp
        import os as _os

        # 수동/자동 불문 실행 시 오늘 날짜 기록 → 스케줄러·재시작 트리거 중복 방지
        try:
            _today_kst = datetime.now(tz=_KST).strftime("%Y-%m-%d")
            _SC_LAST_RUN_FILE.write_text(_today_kst, encoding="utf-8")
        except Exception:
            pass

        # ── 장전 자동 분석: 자동 트리거 + SCREENER_AUTO_SECTOR ON 일 때만 ──────
        sc_market = "kospi"
        _analysis_note = ""   # 장전 분석 요약 — 스크리너 완료 알림에 합쳐 1회만 전송
        if auto:
            _acfg = _read_screener_cfg()
            if _acfg["auto_sector"]:
                try:
                    # pykrx sys.exit 위험 격리 위해 subprocess 로 실행 후 JSON 파싱
                    import json as _json
                    _ma_root = Path(__file__).resolve().parents[2]
                    _ma_cmd = [
                        sys.executable, str(_ma_root / "market_analysis.py"),
                        "--rs-days",      str(_acfg["rs_days"]),
                        "--universe-top", str(_acfg["universe_top"]),
                        "--min-stocks",   str(_acfg["min_stocks"]),
                        "--json",
                    ]
                    _ma_env = _os.environ.copy()
                    _ma_env["PYTHONIOENCODING"] = "utf-8"
                    _ma_proc = _sp.run(
                        _ma_cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace",
                        env=_ma_env, timeout=600, cwd=str(_ma_root),  # 코스피200+코스닥200 → 넉넉히 10분
                    )
                    _ma_out = _ma_proc.stdout
                    _ma_err = _ma_proc.stderr
                    if "ANALYSIS_JSON_BEGIN" not in _ma_out:
                        # subprocess가 마커 없이 종료 → stderr에 실제 traceback 있음
                        _err_detail = (_ma_err or _ma_out or "(출력 없음)")[-400:]
                        raise RuntimeError(f"분석 subprocess 출력 파싱 실패: {_err_detail}")
                    _j = _ma_out.split("ANALYSIS_JSON_BEGIN", 1)[1]
                    _j = _j.split("ANALYSIS_JSON_END", 1)[0].strip()
                    _res = _json.loads(_j)
                    # 분석 내부 오류 포함 여부 확인 (market_analysis.py가 오류를 JSON에 담은 경우)
                    if _res.get("error"):
                        logger.warning("장전 분석 내부 오류: {}", _res["error"][:300])
                        _analysis_note = f"🔎 **장전 분석** — ⚠️ 내부 오류 → 기본 설정으로 진행\n```{_res['error'][:200]}```"
                        # top_sector가 없으면 그냥 기본값 유지
                        if not _res.get("top_sector"):
                            raise RuntimeError("내부 오류로 섹터 미선정")
                    _reg = _res["regime"]
                    _ts = _res["top_sector"]
                    _reg_kr = {"up": "상승장", "down": "하락장",
                               "unknown": "판정불가"}[_reg["regime"]]
                    if _ts:
                        sector = _ts          # 최강 섹터 1개로 교체
                        sc_market = "all"     # 코스피+코스닥 합쳐서 분석
                        # 웹 '산업 필터'에도 반영 → 오늘 어떤 섹터가 선정됐는지 표시
                        try:
                            _ov = ENV_PATH.parent / ".env.overrides"
                            _txt = _ov.read_text(encoding="utf-8") if _ov.exists() else ""
                            _pat = r"^(SCREENER_SECTOR\s*=).*$"
                            _new, _n = _re.subn(_pat, f"SCREENER_SECTOR={_ts}",
                                                _txt, flags=_re.MULTILINE)
                            _ov.write_text(
                                _new if _n > 0 else _txt.rstrip() + f"\nSCREENER_SECTOR={_ts}\n",
                                encoding="utf-8",
                            )
                        except Exception as _e2:
                            logger.warning("SCREENER_SECTOR 자동 반영 실패: {}", _e2)
                    if _reg["regime"] == "down" and _acfg["downtrend_halve"]:
                        top_n = max(1, top_n // 2)   # 하락장 → 종목 수 절반
                    _rk = _res["ranking"][:5]
                    _rk_str = ", ".join(
                        f"{s['sector']}({s['avg_rs']:+.1f}%, {s['count']})" for s in _rk
                    ) or "(없음)"
                    _ks = _reg.get("kospi") or {}
                    _kq = _reg.get("kosdaq") or {}
                    _idx_str = (
                        f"코스피 {_ks.get('gap_pct', 0):+.1f}% / "
                        f"코스닥 {_kq.get('gap_pct', 0):+.1f}%"
                    )
                    _analysis_note = (
                        f"🔎 **장전 분석** — {_reg_kr} "
                        f"(평균 {_reg['gap_pct']:+.1f}% vs 20일선 | {_idx_str})\n"
                        f"섹터분석 최강 섹터: **{_ts or '(없음)'}**\n"
                        f"섹터 강도 TOP5: {_rk_str}"
                    )
                    logger.info("장전 분석 완료: regime={} top_sector={} top_n={} market={}",
                                _reg["regime"], _ts, top_n, sc_market)
                except _sp.TimeoutExpired:
                    # 10분 초과 → 자식 프로세스 종료됨, 기본 설정값으로 폴백
                    logger.warning("장전 분석 타임아웃(600s) — 기본 설정으로 진행")
                    _analysis_note = "🔎 **장전 분석** — ⚠️ 타임아웃(10분 초과) → 기본 설정으로 진행"
                except Exception as _e:
                    logger.warning("장전 분석 실패 — 기본 설정으로 진행: {}", _e)
                    _analysis_note = f"🔎 **장전 분석** — ⚠️ 실패({_e}) → 기본 설정으로 진행"

        root = Path(__file__).resolve().parents[2]
        sc_script = root / "screener.py"
        effective_market_top = 200 if sector else market_top
        effective_workers    = 2   if sector else 4
        cmd = [
            sys.executable, str(sc_script),
            "--mode", "weekly",
            "--market", sc_market,
            "--market-top", str(effective_market_top),
            "--top", str(top_n),
            "--dry-run",
            "--workers", str(effective_workers),
        ]
        if sector:
            cmd += ["--sector", sector]
        env = _os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            _SC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

            # ── 실행 시작 표시 줄을 스트림 버퍼에 추가 ───────────────────────────
            if len(_SC_STREAM_BUF) > _SC_STREAM_MAX:
                del _SC_STREAM_BUF[:-1000]  # 오래된 줄 정리 (최근 1000줄 유지)
            _SC_STREAM_BUF.append(f"━━━ 새 스크리너 실행  {_time.strftime('%Y-%m-%d %H:%M:%S')} ━━━")
            _SC_STREAM_BUF.append("[시작 중... 패키지 로딩 약 30~60초 소요]")

            proc = _sp.Popen(
                cmd,
                stdout=_sp.PIPE, stderr=_sp.STDOUT,
                cwd=str(root), env=env,
                bufsize=1, text=True, encoding="utf-8", errors="replace",
            )

            # Reader thread: PIPE 한 줄씩 → _captured_lines(결과 파싱용) + _SC_STREAM_BUF(SSE용) + 파일(선택)
            _captured_lines: list[str] = []
            def _pipe_reader():
                try:
                    for _line in proc.stdout:
                        _captured_lines.append(_line)
                        _SC_STREAM_BUF.append(_line.rstrip())   # SSE 메모리 버퍼 (항상 성공)
                        try:
                            with _SC_LOG_PATH.open("a", encoding="utf-8", errors="replace") as _lf:
                                _lf.write(_line)
                        except Exception as _write_err:
                            logger.warning("스크리너 로그 파일 쓰기 실패: {}", _write_err)
                except Exception as _read_err:
                    logger.warning("스크리너 PIPE 읽기 오류: {}", _read_err)

            _reader_t = threading.Thread(target=_pipe_reader, daemon=True)
            _reader_t.start()

            try:
                proc.wait(timeout=1800)
            except _sp.TimeoutExpired:
                proc.kill()
                proc.wait()
                _reader_t.join(timeout=10)
                _SC_JOBS[job_id].update({"status": "error", "output": "타임아웃 (1800초 초과)"})
                return

            # subprocess 종료 후 reader thread 가 남은 출력 처리 대기
            _reader_t.join(timeout=30)

            # 진단: 서브프로세스 종료 코드 + 캡쳐 크기 로깅
            _rc = proc.returncode
            output = "".join(_captured_lines) or "(출력 없음)"
            logger.info(
                "스크리너 종료 [{}]: exit_code={} captured_lines={} captured_chars={}",
                job_id, _rc, len(_captured_lines), len(output)
            )
            # 장전 분석 요약 + 스크리너 결과를 한 메시지로 합쳐 전송
            _sep = "\n────────────\n"
            _note_prefix = (_analysis_note + _sep) if _analysis_note else ""
            # "선별 N개: A,B,C" 파싱 → SYMBOLS 자동 업데이트 (dry run이면 스킵)
            m = _re.search(r"선별\s*\d+개:\s*((?:[A-Z0-9]+\.K[SQ](?:,\s*)?)+)", output)
            if m:
                # suffix 제거 후 6자리 코드로 통일 (000660.KS → 000660)
                symbols = ",".join(s.split(".")[0] for s in m.group(1).replace(" ", "").split(",") if s)
                symbols = _merge_positions_into_symbols(symbols)
                # 선별 종목명 조회 (dry run·실거래 공통)
                sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
                sym_names = [get_name(s) or s for s in sym_list]
                sym_display = " · ".join(
                    f"{nm}({cd})" for nm, cd in zip(sym_names, sym_list)
                )
                if settings.trade_dry_run:
                    logger.info("스크리너 결과 확인 (dry run — SYMBOLS 미업데이트): {}", symbols)
                    notify(
                        f"{_note_prefix}"
                        f"📊 **스크리너 완료(검증모드)** — {sector or '전체'} TOP{top_n}\n"
                        f"선별 종목: {sym_display}\n"
                        f"(검증모드 — 운용 종목 미반영)"
                    )
                else:
                    override_path = ENV_PATH.parent / ".env.overrides"
                    text = override_path.read_text(encoding="utf-8") if override_path.exists() else ""
                    pat = r"^(SYMBOLS\s*=).*$"
                    new_text, n = _re.subn(pat, rf"SYMBOLS={symbols}", text, flags=_re.MULTILINE)
                    text = new_text if n > 0 else text.rstrip() + f"\nSYMBOLS={symbols}\n"
                    override_path.write_text(text, encoding="utf-8")
                    settings.trade_symbols = symbols
                    logger.info("스크리너 SYMBOLS 자동 업데이트 (포지션 병합): {}", symbols)
                    notify(
                        f"{_note_prefix}"
                        f"📊 **스크리너 완료** — {sector or '전체'} TOP{top_n}\n"
                        f"선별 종목: {sym_display}\n"
                        f"운용 종목 자동 업데이트 완료"
                    )
            else:
                notify(f"{_note_prefix}📊 스크리너 완료 — 매칭 종목 없음 (섹터: {sector or '전체'})")
            _SC_JOBS[job_id].update({"status": "done", "output": output})
        except Exception as e:
            _SC_JOBS[job_id].update({"status": "error", "output": str(e)})
            _ep = (_analysis_note + "\n────────────\n") if _analysis_note else ""
            notify(f"{_ep}⚠️ 스크리너 오류: {e}")

    # ── 스크리너 자동 실행: 재시작 시 + 매주 월요일 8:30 KST ────────────────────
    _SC_LAST_RUN_FILE = (
        Path("/app/data/screener_last_run.txt")
        if Path("/app/data").exists()
        else Path(__file__).resolve().parents[2] / "data" / "screener_last_run.txt"
    )
    _SC_LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _screener_is_running() -> bool:
        """이미 실행 중인 스크리너 job이 있으면 True."""
        return any(j.get("status") == "running" for j in _SC_JOBS.values())

    def _trigger_screener_auto(reason: str) -> str:
        """설정에서 스크리너 파라미터 읽어 자동 실행. job_id 반환.
        이미 실행 중인 job이 있으면 스킵하고 해당 job_id 반환.
        Lock으로 race condition 방지."""
        with _SC_LOCK:
            if _screener_is_running():
                running_id = next(
                    jid for jid, j in _SC_JOBS.items() if j.get("status") == "running"
                )
                logger.info("스크리너 이미 실행 중 — 중복 시작 스킵 [{}]: job={}", reason, running_id)
                return running_id

            cfg = _read_screener_cfg()
            job_id = uuid.uuid4().hex
            _SC_JOBS[job_id] = {
                "status": "running", "output": "",
                "sector": cfg["sector"], "top_n": cfg["top_n"], "market_top": cfg["market_top"],
                "started_at": _time.time(),
            }
            threading.Thread(
                target=_run_sc_job,
                args=(job_id, cfg["sector"], cfg["top_n"], cfg["market_top"]),
                kwargs={"auto": True},
                daemon=True,
            ).start()
            logger.info("스크리너 자동 실행 [{}]: sector={} top_n={} job={}",
                        reason, cfg["sector"], cfg["top_n"], job_id)
            return job_id

    # 재시작 시: 평일이고, 아직 오늘 실행 안 했으며, 장 시작 전(09:00 KST 이전)일 때만 실행
    # ※ 장중 재시작(OOM 등)에서는 스크리너를 다시 돌리지 않음
    try:
        _now2 = datetime.now(tz=_KST)
        _today = _now2.strftime("%Y-%m-%d")
        _last  = _SC_LAST_RUN_FILE.read_text(encoding="utf-8").strip() if _SC_LAST_RUN_FILE.exists() else ""
        from stock_bot.market_calendar import is_trading_day as _is_trading_day
        _is_open = _is_trading_day(_now2)  # 거래일(주말·공휴일·임시휴장 제외)
        _before_market = _now2.hour < 9   # 09:00 KST 이전에만 재시작 트리거
        if _is_open and _last != _today and _before_market:
            _SC_LAST_RUN_FILE.write_text(_today, encoding="utf-8")
            _trigger_screener_auto("거래일 재시작")
        elif _is_open and _last != _today and not _before_market:
            logger.info("스크리너 재시작 트리거 스킵 — 장중 재시작 ({} KST, 09:00 이후)", _now2.strftime("%H:%M"))
    except Exception as _e:
        logger.warning("스크리너 시작 시 자동 실행 실패: {}", _e)

    # 평일 매일 08:00 KST 스케줄러 (07:58~08:02 윈도우)
    def _screener_scheduler():
        from stock_bot.market_calendar import is_trading_day as _is_trading_day
        while True:
            _time.sleep(30)
            try:
                now = datetime.now(tz=_KST)
                # 거래일 08:00 KST — 57~02분 윈도우로 30초 슬립 오차 흡수 (공휴일·임시휴장 제외)
                if _is_trading_day(now) and now.hour == 8 and now.minute <= 2:
                    today_str = now.strftime("%Y-%m-%d")
                    last_str = _SC_LAST_RUN_FILE.read_text(encoding="utf-8").strip() if _SC_LAST_RUN_FILE.exists() else ""
                    if last_str != today_str:
                        _SC_LAST_RUN_FILE.write_text(today_str, encoding="utf-8")
                        _trigger_screener_auto(f"평일 자동 실행 08:00 ({now.strftime('%a')})")
            except Exception as _e:
                logger.warning("스크리너 스케줄러 오류: {}", _e)

    threading.Thread(target=_screener_scheduler, daemon=True, name="screener-scheduler").start()

    @app.post("/api/screener")
    def api_screener(req: ScreenerRequest):
        """스크리너를 백그라운드 스레드로 시작하고 job_id 반환."""
        with _SC_LOCK:
            if _screener_is_running():
                running_id = next(
                    jid for jid, j in _SC_JOBS.items() if j.get("status") == "running"
                )
                return JSONResponse({"ok": True, "job_id": running_id, "already_running": True})
            job_id = uuid.uuid4().hex
            _SC_JOBS[job_id] = {
                "status": "running", "output": "",
                "sector": req.sector, "top_n": req.top_n, "market_top": req.market_top,
                "started_at": _time.time(),
            }
            t = threading.Thread(
                target=_run_sc_job,
                args=(job_id, req.sector, req.top_n, req.market_top),
                daemon=True,
            )
            t.start()
        return JSONResponse({"ok": True, "job_id": job_id})

    @app.post("/api/screener/run")
    def api_screener_run():
        """현재 .env.overrides 스크리너 설정 그대로 즉시 실행 (파라미터 변경 불필요)."""
        job_id = _trigger_screener_auto("수동 실행")
        return JSONResponse({"ok": True, "job_id": job_id})

    @app.get("/api/screener/jobs")
    def api_screener_jobs():
        """전체 _SC_JOBS 목록 — 중복 트리거 진단용. 출력 본문은 길이만 표시."""
        jobs = []
        for jid, j in sorted(
            _SC_JOBS.items(),
            key=lambda kv: kv[1].get("started_at", 0),
            reverse=True,
        ):
            jobs.append({
                "job_id": jid,
                "status": j.get("status"),
                "started_at": j.get("started_at"),
                "sector": j.get("sector"),
                "top_n": j.get("top_n"),
                "output_len": len(j.get("output", "")),
            })
        return JSONResponse({"count": len(jobs), "jobs": jobs})

    @app.post("/api/screener/{job_id}/cancel")
    def api_screener_cancel(job_id: str):
        """실행 중인 스크리너 job 강제 종료 — 컨테이너 내 screener.py 프로세스 죽임."""
        import subprocess as _sp_cancel
        job = _SC_JOBS.get(job_id)
        if not job:
            return JSONResponse({"ok": False, "error": "job not found"})
        try:
            # 컨테이너 내부에서 screener.py 모든 인스턴스 종료
            _sp_cancel.run(["pkill", "-9", "-f", "screener.py"], timeout=5)
            _SC_JOBS[job_id].update({"status": "error", "output": "사용자 취소"})
            return JSONResponse({"ok": True, "killed": True})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})

    @app.get("/api/screener/latest")
    def api_screener_latest():
        """가장 최근 스크리너 job 반환 — 페이지 새로고침 시 자동 재연결용."""
        if not _SC_JOBS:
            return JSONResponse({"job_id": None})
        latest_id = max(_SC_JOBS, key=lambda jid: _SC_JOBS[jid].get("started_at", 0))
        job = _SC_JOBS[latest_id]
        age = _time.time() - job.get("started_at", 0)
        if job["status"] != "running" and age > 3600:
            return JSONResponse({"job_id": None})
        return JSONResponse({
            "job_id": latest_id,
            "status": job["status"],
            "output": job["output"],
        })

    @app.get("/api/screener/{job_id}")
    def api_screener_status(job_id: str):
        """스크리너 job 상태/결과 조회."""
        job = _SC_JOBS.get(job_id)
        if job is None:
            return JSONResponse({"status": "not_found", "output": ""})
        return JSONResponse({"status": job["status"], "output": job["output"]})

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request):
        template_path = Path(__file__).parent / "templates" / "logs.html"
        return HTMLResponse(template_path.read_text(encoding="utf-8"))

    @app.get("/api/logs/stream")
    async def logs_stream(source: str = "bot"):
        """SSE: stock_bot.log / stock_web.log / screener(메모리 버퍼) 실시간 스트리밍."""
        if source == "screener":
            # ── 스크리너: 파일 대신 in-memory 버퍼(_SC_STREAM_BUF)에서 직접 읽음 ──
            # 파일 I/O 실패와 무관하게 항상 출력 표시.
            async def generate():
                try:
                    # 최근 200줄 전송 (cursor 기반 — 재연결 시 중복 없음)
                    cursor = max(0, len(_SC_STREAM_BUF) - 200)
                    for line in _SC_STREAM_BUF[cursor:]:
                        yield f"data: {line}\n\n"
                    cursor = len(_SC_STREAM_BUF)

                    idle_ticks = 0
                    while True:
                        if len(_SC_STREAM_BUF) > cursor:
                            for line in _SC_STREAM_BUF[cursor:]:
                                yield f"data: {line}\n\n"
                            cursor = len(_SC_STREAM_BUF)
                            idle_ticks = 0
                        else:
                            idle_ticks += 1
                            if idle_ticks % 15 == 0:
                                yield "data: \n\n"  # heartbeat
                            await asyncio.sleep(1)
                except Exception as e:
                    yield f"data: [오류: {e}]\n\n"

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # ── 봇/웹 로그: 파일 tail ────────────────────────────────────────────────
        if source == "web":
            log_path = Path("/app/logs/stock_web.log")
        else:
            log_path = Path("/app/logs/stock_bot.log")

        async def generate():
            try:
                # 파일이 없으면 최대 60초 대기 (서버 시작 지연 고려)
                waited = 0
                while not log_path.exists():
                    yield "data: \n\n"  # heartbeat — 연결 유지
                    await asyncio.sleep(2)
                    waited += 2
                    if waited >= 60:
                        yield f"data: [로그 파일 없음: {log_path.name}]\n\n"
                        return

                # 최근 200줄 먼저 전송 — 서브줄(VWAP/RSI/BB 등) 포함 전체 전송
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                for line in lines[-200:]:
                    stripped = line.rstrip()
                    if stripped:
                        yield f"data: {stripped}\n\n"

                # 이후 새 줄 tail
                f = open(log_path, "r", encoding="utf-8", errors="replace")
                f.seek(0, 2)  # EOF 로 이동
                idle_ticks = 0
                try:
                    while True:
                        line = f.readline()
                        if line:
                            idle_ticks = 0
                            yield f"data: {line.rstrip()}\n\n"
                        else:
                            idle_ticks += 1
                            if idle_ticks % 15 == 0:
                                yield "data: \n\n"  # heartbeat
                            await asyncio.sleep(1)
                finally:
                    f.close()
            except Exception as e:
                yield f"data: [오류: {e}]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/symbol-names")
    def api_symbol_names(symbols: str = ""):
        """심볼 코드 → 이름 변환 (브로커 불필요). symbols: 쉼표 구분 코드."""
        syms = [s.strip() for s in symbols.split(",") if s.strip()]
        return JSONResponse([{"symbol": s, "name": get_name(s)} for s in syms])

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/ping")
    def ping():
        """헬스체크. 재빌드 후 서버 복구 감지용."""
        return {"ok": True}

    @app.get("/api/symbols")
    def api_symbols():
        """현재 운용 종목 목록 (초경량). 대시보드 1초 폴링용."""
        syms = settings.symbols  # 6자리 코드 리스트
        return JSONResponse({
            "symbols": syms,
            "names": {s: get_name(s) for s in syms},
        })

    @app.get("/api/perf")
    def api_perf():
        """누적 성과 조회 (실현손익 + 브로커 평가 기준 net_pnl)."""
        perf = _realized_pnl_summary()
        # 초기 페이지 렌더와 동일한 net_pnl 계산 (브로커 평가 - 초기자본)
        # 이게 없으면 페이지 새로고침 시 net_pnl → realized_pnl 로 숫자 점프
        account = _account_summary()
        initial = settings.initial_capital_krw
        if account.get("available") and account.get("total_eval", 0) > 0 and initial > 0:
            net_pnl = account["total_eval"] - initial
            perf["net_pnl"] = net_pnl
            perf["net_pnl_pct"] = net_pnl / initial * 100
            perf["net_pnl_available"] = True
        else:
            perf["net_pnl"] = 0.0
            perf["net_pnl_pct"] = 0.0
            perf["net_pnl_available"] = False
        return JSONResponse(perf)

    _quotes_cache: dict = {"ts": 0.0, "data": []}

    @app.get("/api/quotes")
    def api_quotes():
        """종목별 현재가 조회. 15초 캐시로 KIS 인증 반복 방지."""
        import time
        from datetime import datetime
        from stock_bot.broker.kis import KISBroker
        from stock_bot.market_calendar import is_trading_day
        # 휴장일에는 직전 종가만 반복 조회되므로 시세를 표시하지 않음
        if not is_trading_day(datetime.now()):
            return JSONResponse({"market_closed": True, "quotes": []})
        now = time.monotonic()
        if now - _quotes_cache["ts"] < 15 and _quotes_cache["data"]:
            return JSONResponse({"quotes": _quotes_cache["data"]})
        results = []
        broker = None
        try:
            broker = KISBroker()
            for sym in settings.symbols:
                try:
                    q = broker.get_quote(sym)
                    from stock_bot.names import get_name
                    results.append({
                        "symbol": sym,
                        "name": get_name(sym),
                        "price": q.price,
                        "change_pct": q.change_pct,
                    })
                except Exception as e:
                    results.append({"symbol": sym, "name": sym, "price": None, "change_pct": None, "error": str(e)})
            _quotes_cache["ts"] = now
            _quotes_cache["data"] = results
        except Exception as e:
            return JSONResponse({"error": str(e), "quotes": _quotes_cache["data"]})
        finally:
            if broker:
                broker.close()
        return JSONResponse({"quotes": results})

    @app.get("/api/account")
    def api_account():
        """자산 현황 조회 (캐시 사용)."""
        return JSONResponse(_account_summary())

    @app.post("/api/account/refresh")
    def api_account_refresh():
        """캐시 무시하고 KIS 에서 잔고 재조회."""
        return JSONResponse(_account_summary(force=True))

    @app.get("/api/config")
    def get_config():
        return JSONResponse({"dry_run": settings.trade_dry_run})

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
        if payload.initial_capital is not None:
            if payload.initial_capital < 0:
                raise HTTPException(400, "initial_capital must be >= 0")
            settings.initial_capital_krw = payload.initial_capital  # type: ignore[assignment]
            _update_override_key("INITIAL_CAPITAL_KRW", str(int(payload.initial_capital)))
            logger.info("초기자금 변경: {}원 → .env.overrides 반영", int(payload.initial_capital))
        if payload.fee_buy_pct is not None:
            settings.trade_fee_buy_pct = payload.fee_buy_pct  # type: ignore[assignment]
            _update_override_key("TRADE_FEE_BUY_PCT", str(payload.fee_buy_pct))
        if payload.fee_sell_pct is not None:
            settings.trade_fee_sell_pct = payload.fee_sell_pct  # type: ignore[assignment]
            _update_override_key("TRADE_FEE_SELL_PCT", str(payload.fee_sell_pct))
        if not updates:
            if payload.dry_run is None and payload.initial_capital is None and payload.fee_buy_pct is None and payload.fee_sell_pct is None:
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
