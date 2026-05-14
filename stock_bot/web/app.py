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
from datetime import datetime, timedelta, timezone
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

_KST = timezone(timedelta(hours=9))


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

    @app.get("/params", response_class=HTMLResponse)
    def params_page(request: Request):
        template_path = Path(__file__).parent / "templates" / "params.html"
        resp = HTMLResponse(template_path.read_text(encoding="utf-8"))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    @app.get("/api/params")
    def api_get_params():
        """현재 .env.overrides 파라미터 읽기."""
        import re as _re
        override_path = ENV_PATH.parent / ".env.overrides"
        result: dict[str, str] = {}
        if override_path.exists():
            for line in override_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    v = v.split("#")[0].strip()  # 인라인 주석 제거
                    result[k.strip()] = v
        return JSONResponse(result)

    class ParamUpdate(BaseModel):
        updates: dict[str, str]

    ALLOWED_PARAM_KEYS = {
        "ENSEMBLE_WEIGHTS", "ENSEMBLE_BUY_THRESHOLD", "ENSEMBLE_SELL_THRESHOLD",
        "ENSEMBLE_MIN_BUY_VOTES", "ENSEMBLE_MIN_SELL_VOTES",
        "TRADE_VWAP_BAND", "TRADE_VWAP_SELL_BAND", "TRADE_VWAP_ST_BULL_SELL_BAND", "TRADE_VWAP_WARMUP_BARS",
        "TRADE_RSI_PERIOD", "TRADE_RSI_OVERSOLD", "TRADE_RSI_OVERBOUGHT",
        "TRADE_SUPERTREND_PERIOD", "TRADE_SUPERTREND_MULT",
        "ADD_BUY_ENABLED", "ADD_BUY_THRESHOLD", "ADD_BUY_MIN_VOTES",
        "ADD_BUY_MAX_COUNT", "ADD_BUY_FRACTION", "ADD_BUY_MAX_POSITION_PCT",
        "ATR_STOP_LOSS_ENABLED", "ATR_PERIOD", "ATR_STOP_MULTIPLIER", "ATR_STOP_MAX_PCT",
        "ENSEMBLE_VOLUME_FILTER_ENABLED", "ENSEMBLE_VOLUME_MA_PERIOD",
        "ENSEMBLE_VOLUME_HIGH_RATIO", "ENSEMBLE_VOLUME_LOW_RATIO",
        "ENSEMBLE_VOLUME_SCORE_BOOST", "ENSEMBLE_VOLUME_SCORE_PENALTY",
        "ENTRY_BLOCK_ENABLED", "ENTRY_BLOCK_START", "ENTRY_BLOCK_END",
        "ENTRY_BLOCK_MIN_PROFIT_TO_SELL_PCT", "ENTRY_BLOCK_FORCE_SELL_FRACTION",
        "POSITION_SIZING", "POSITION_FRACTION",
    }

    @app.post("/api/params")
    def api_save_params(body: ParamUpdate):
        """변경된 파라미터를 .env.overrides에 저장."""
        import re as _re
        override_path = ENV_PATH.parent / ".env.overrides"
        safe = {k: v for k, v in body.updates.items() if k in ALLOWED_PARAM_KEYS}
        if not safe:
            return JSONResponse({"ok": False, "error": "no allowed keys"})
        text = override_path.read_text(encoding="utf-8") if override_path.exists() else ""
        for key, val in safe.items():
            pattern = rf"^({_re.escape(key)}\s*=).*$"
            new_text, n = _re.subn(pattern, rf"\g<1>{val}", text, flags=_re.MULTILINE)
            text = new_text if n > 0 else text.rstrip() + f"\n{key}={val}\n"
        override_path.write_text(text, encoding="utf-8")
        logger.info("파라미터 웹 UI 저장: {}", list(safe.keys()))
        return JSONResponse({"ok": True, "saved": list(safe.keys())})

    class BacktestRequest(BaseModel):
        symbol: str = "005930.KS"
        period: str = "60d"

    # ── 백테스트 job 저장소 (메모리) ────────────────────────────────────────────
    _BT_JOBS: dict[str, dict] = {}  # job_id → {status, output, started_at}

    def _run_bt_job(job_id: str, symbols: str, period: str) -> None:
        """별도 스레드에서 backtest_current.py 실행 후 결과를 _BT_JOBS 에 저장."""
        import subprocess as _sp
        bt_script = Path(__file__).resolve().parents[2] / "backtest_current.py"
        try:
            result = _sp.run(
                [sys.executable, str(bt_script), symbols, period],
                capture_output=True, text=True, timeout=300,
                cwd=str(Path(__file__).resolve().parents[2]),
            )
            output = result.stdout or result.stderr or "(출력 없음)"
            _BT_JOBS[job_id] = {"status": "done", "output": output}
        except _sp.TimeoutExpired:
            _BT_JOBS[job_id] = {"status": "error", "output": "타임아웃 (300초 초과)"}
        except Exception as e:
            _BT_JOBS[job_id] = {"status": "error", "output": str(e)}

    @app.post("/api/backtest")
    def api_backtest(req: BacktestRequest):
        """백테스트를 백그라운드 스레드로 시작하고 job_id 반환."""
        from stock_bot.names import resolve_symbol
        symbols = ",".join(resolve_symbol(s) for s in req.symbol.split(","))
        job_id = uuid.uuid4().hex
        _BT_JOBS[job_id] = {"status": "running", "output": ""}
        t = threading.Thread(target=_run_bt_job, args=(job_id, symbols, req.period), daemon=True)
        t.start()
        return JSONResponse({"ok": True, "job_id": job_id})

    @app.get("/api/backtest/{job_id}")
    def api_backtest_status(job_id: str):
        """백테스트 job 상태/결과 조회."""
        job = _BT_JOBS.get(job_id)
        if job is None:
            return JSONResponse({"status": "not_found", "output": ""})
        return JSONResponse({"status": job["status"], "output": job["output"]})

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request):
        template_path = Path(__file__).parent / "templates" / "logs.html"
        return HTMLResponse(template_path.read_text(encoding="utf-8"))

    @app.get("/api/logs/stream")
    async def logs_stream(source: str = "bot"):
        """SSE: stock_bot.log / stock_web.log 를 실시간으로 스트리밍."""
        log_path = Path("/app/logs/stock_web.log" if source == "web" else "/app/logs/stock_bot.log")

        async def generate():
            try:
                # 파일이 없으면 최대 30초 대기
                waited = 0
                while not log_path.exists():
                    if waited == 0:
                        label = "웹" if source == "web" else "봇"
                        yield f"data: [{label} 로그 파일 대기 중...]\n\n"
                    await asyncio.sleep(2)
                    waited += 2
                    if waited >= 30:
                        yield f"data: [로그 파일 없음: {log_path}]\n\n"
                        return

                # 최근 300줄 먼저 전송
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                for line in lines[-300:]:
                    yield f"data: {line.rstrip()}\n\n"

                # 이후 새 줄 tail (로테이션 감지 포함)
                f = open(log_path, "r", encoding="utf-8", errors="replace")
                f.seek(0, 2)  # EOF 로 이동
                try:
                    while True:
                        line = f.readline()
                        if line:
                            yield f"data: {line.rstrip()}\n\n"
                        else:
                            await asyncio.sleep(1)
                            # 로테이션 감지: 파일 크기가 현재 위치보다 작으면 새 파일 생성됨
                            try:
                                if log_path.stat().st_size < f.tell():
                                    f.close()
                                    f = open(log_path, "r", encoding="utf-8", errors="replace")
                            except OSError:
                                pass
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

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/ping")
    def ping():
        """헬스체크. 재빌드 후 서버 복구 감지용."""
        return {"ok": True}

    @app.get("/api/perf")
    def api_perf():
        """누적 성과 조회 (실현손익·수익률·거래횟수)."""
        return JSONResponse(_realized_pnl_summary())

    _quotes_cache: dict = {"ts": 0.0, "data": []}

    @app.get("/api/quotes")
    def api_quotes():
        """종목별 현재가 조회. 15초 캐시로 KIS 인증 반복 방지."""
        import time
        from stock_bot.broker.kis import KISBroker
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
