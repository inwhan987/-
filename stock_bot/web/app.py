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
from datetime import datetime
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from stock_bot.config import settings
from stock_bot.names import get_name
from stock_bot.notify import notify
from stock_bot.news.store import init_news_db
from stock_bot.storage.db import init_db

# 데이터 계층(DB 조회·브로커 상태·대장주 표시) 은 services 로 분리.
# 라우트 클로저가 그대로 참조하도록 이름을 app 네임스페이스로 가져온다.
# _POSITIONS_CACHE/_ACCOUNT_CACHE 는 in-place 변형되는 dict 라 동일 객체 공유.
from stock_bot.web.services import (
    _account_summary,
    _apply_strategy_split,
    _get_broker,
    _leader_today,
    _live_positions,
    _merge_positions_into_symbols,
    _realized_pnl_summary,
    _recent_news,
    _recent_reviews,
    _recent_trades,
    _sentiment_summary,
    _POSITIONS_CACHE,
    _POSITIONS_CACHE_TTL,
)

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
    stock_capital: float | None = Field(default=None)
    leader_capital: float | None = Field(default=None)
    fee_buy_pct: float | None = Field(default=None)
    fee_sell_pct: float | None = Field(default=None)


class BacktestRequest(BaseModel):
    symbol: str = "005930.KS"
    period: str = "60d"


class ScreenerRequest(BaseModel):
    sector: str = "IT"
    top_n: int = 3
    market_top: int = 1000


class ParamUpdate(BaseModel):
    updates: dict[str, str]


class HolidayUpdate(BaseModel):
    dates: list[str]

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def create_app() -> FastAPI:
    _setup_uvicorn_log_intercept()
    init_db()
    init_news_db()
    # 봇과 동일한 핫리로드 — .env.overrides 변경을 웹 settings 에도 1초 내 반영.
    # 이 워처가 없으면 파라미터 탭에서 바꿔도 웹 프로세스의 settings 는 기동 시점
    # 값에 고정돼 대시보드(대장주 ON/OFF 배지 등)에 반영되지 않는다.
    try:
        from stock_bot.live.runner import _start_env_watcher
        _start_env_watcher("all")  # 웹은 대시보드 표시용이라 전 키 반영
    except Exception as exc:
        logger.warning("env watcher 시작 실패 (웹): {}", exc)
    app = FastAPI(title="stock-bot dashboard")
    static_dir = BASE / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── 세션 로그인 (2026-07-16, 캐디 Basic Auth 대체) ────────────────────────
    # 이 회선의 중간 장비(LG 허브/DMZ)는 401 을 상시 응답하는 포트를 "수상한 포트"로
    # 학습해 브라우저 트래픽만 골라 지연/리셋한다(캐디 Basic Auth 가 그렇게 당함 —
    # 실측 상세는 memory/외부접속 기록). 그래서 인증 실패에도 302/200 만 응답하는
    # 앱 자체 로그인으로 비밀번호를 건다. 401/403 응답 재도입 금지.
    # WEB_PASSWORD 는 파이 로컬 .env 전용(git 추적 금지). 미설정이면 인증 비활성.
    # 비밀번호 변경 시 stock-web 재시작 필요(기동 시점에 1회 읽음).
    import hashlib as _hashlib
    import hmac as _hmac
    import os as _os
    from urllib.parse import quote as _urlquote

    _web_pw = (_os.environ.get("WEB_PASSWORD") or "").strip()
    _session_secret = _hashlib.sha256(("stock-web-session:" + _web_pw).encode()).digest()
    _SESSION_TTL = 30 * 24 * 3600  # 30일
    # 읽기전용 머신 토큰(파이 로컬 .env 전용, git 추적 금지). 미설정이면 비활성.
    # GET 요청에 한해 헤더 X-Web-Token 이 일치하면 세션 없이 통과 — 로컬 동기화·
    # 로그 확인용. GET-only 라서 토큰이 새도 설정 변경(POST)은 불가 → Funnel 로
    # 노출돼도 읽기 피해만. 출발지 IP 를 안 보므로 프록시/터널 토폴로지 무관.
    _read_token = (_os.environ.get("WEB_READ_TOKEN") or "").strip()
    # 인증 예외: 로그인 자체·정적파일·헬스체크·CI 인제스트(자체 시크릿 검증)
    _AUTH_EXEMPT = {"/login", "/api/login", "/healthz", "/ping", "/api/screener/ingest"}

    def _session_sign(ts: str) -> str:
        return _hmac.new(_session_secret, ts.encode(), _hashlib.sha256).hexdigest()

    def _session_valid(token: str | None) -> bool:
        if not token or "." not in token:
            return False
        ts, sig = token.split(".", 1)
        if not ts.isdigit() or int(ts) < time.time():
            return False
        return _hmac.compare_digest(_session_sign(ts), sig)

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        if not _web_pw:
            return await call_next(request)
        path = request.url.path
        if path in _AUTH_EXEMPT or path.startswith("/static/"):
            return await call_next(request)
        if _session_valid(request.cookies.get("web_session")):
            return await call_next(request)
        # 읽기전용 토큰: GET 에 한해 세션 없이 허용(동기화·로그 확인용).
        # request.state.read_token 을 세워 핸들러가 시크릿을 마스킹하게 한다
        # (예: /api/params 는 토큰 접근 시 ALLOWED_PARAM_KEYS 운영값만 반환).
        if (
            _read_token
            and request.method == "GET"
            and _hmac.compare_digest(
                request.headers.get("X-Web-Token", "").encode(),
                _read_token.encode(),
            )
        ):
            request.state.read_token = True
            return await call_next(request)
        return RedirectResponse(f"/login?next={_urlquote(path, safe='/')}", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        # 인증 비활성이거나 이미 로그인된 상태면 대시보드로
        if not _web_pw or _session_valid(request.cookies.get("web_session")):
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "login.html", {})

    @app.post("/api/login")
    async def api_login(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        pw = str(body.get("password") or "")
        if _web_pw and _hmac.compare_digest(pw.encode(), _web_pw.encode()):
            ts = str(int(time.time()) + _SESSION_TTL)
            resp = JSONResponse({"ok": True})
            resp.set_cookie(
                "web_session",
                f"{ts}.{_session_sign(ts)}",
                max_age=_SESSION_TTL,
                httponly=True,
                samesite="lax",
            )
            return resp
        await asyncio.sleep(1.0)  # 무차별 대입 지연 (실패도 200 — 중간장비 학습 방지)
        return JSONResponse({"ok": False})

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        trades = _recent_trades()
        news = _recent_news()
        sentiment, news_window = _sentiment_summary()
        # 최초 HTML 렌더는 캐시만 사용 — KIS 동기 호출(get_positions/account)로
        # 페이지 로딩이 막히지 않게 한다. 프론트가 로드 직후 /api/positions·
        # /api/account·/api/perf 를 폴링해 즉시 실값으로 갱신한다.
        positions = _POSITIONS_CACHE["data"] or []
        account = _account_summary(cache_only=True)
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
        # 전략별 순손익(실현+미실현) 분리 — 합이 항상 브로커 총손익과 일치
        leader = _leader_today()
        _apply_strategy_split(perf, positions)
        cfg = {
            "strategy": settings.trade_strategy,
            "sizing": settings.position_sizing,
            "dry_run": settings.trade_dry_run,
            "env": settings.kis_env,
            "symbols": settings.symbols,
            "symbol_names": {s: get_name(s) for s in settings.symbols},
            "candle": settings.live_candle,
            "interval": settings.live_interval_minutes,
            "candle_minutes": settings.live_candle_minutes,
            "news_enabled": settings.news_enabled,
            # 대장주봇 운영환경 (환경=env 는 스톡봇과 공유 — 같은 모의투자 서버)
            "leader_enabled": bool(getattr(settings, "leader_trade_enabled", False)),
            "leader_interval": settings.leader_interval_min,
            "leader_budget": settings.leader_budget_krw,
            "leader_tp": settings.leader_tp_pct,
            "leader_stop": settings.leader_stop_buf_pct,
            "leader_close": settings.leader_close_time,
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
                "leader": leader,
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

    @app.get("/api/index")
    def api_index():
        """대시보드 지수 위젯 — KOSPI/KOSDAQ 현재값·등락·스파크라인·레짐.

        네이버 fchart 일봉(60초 TTL 캐시). 약세장 신규매수 차단 게이트(REGIME_BLOCK)와
        같은 MA·모멘텀 기준(REGIME_MA_PERIOD·REGIME_MOM_DAYS)으로 배지·MA선 표시.
        게이트 활성화 여부도 함께 내려준다.
        """
        from stock_bot.broker.naver_index import market_snapshot
        gate_on = bool(getattr(settings, "regime_block_enabled", False))
        ma_p = int(getattr(settings, "regime_ma_period", 20))
        mom_d = int(getattr(settings, "regime_mom_days", 10))
        return JSONResponse({
            "gate_enabled": gate_on,
            "ma_period": ma_p,
            "mom_days": mom_d,
            "markets": [
                market_snapshot("KOSPI", ma_period=ma_p, mom_days=mom_d),
                market_snapshot("KOSDAQ", ma_period=ma_p, mom_days=mom_d),
            ],
        })

    @app.get("/api/leader/status")
    def api_leader_status():
        """대장주 오늘 상태 카드용 — 바스켓·보유·완료·전략별 실현손익."""
        info = _leader_today()
        lp = _realized_pnl_summary(strategy="leader_pullback")
        info["realized_pnl"] = lp["realized_pnl"]
        # "실현 N건" 은 청산(매도) 횟수 = 완료된 거래 수. total_trades 는
        # 매수+매도 leg 합이라 1회 진입→청산을 2건으로 중복 카운트했음.
        info["trades"] = lp["sell_count"]
        # 장마감(대장주 청산시각 경과) 또는 휴장 → 현황 상세 접고 로그로 갈음(표시 전용)
        from datetime import time as _dtime
        from stock_bot.market_calendar import is_trading_day as _is_td
        _now_k = datetime.now(_KST)
        _closed = not _is_td(_now_k)  # 주말·공휴일·임시휴장
        over = _closed
        if not over:
            try:
                _hh, _mm = str(settings.leader_close_time).split(":")
                over = _now_k.time() >= _dtime(int(_hh), int(_mm))
            except Exception:
                over = False
        info["session_over"] = over
        info["market_closed"] = _closed  # 휴장이면 '오늘 종료' 대신 '휴장일' 표시
        return JSONResponse(info)

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
        # data is None → 조회 실패. []  → 성공·빈 잔고. [...] → 성공·보유.
        if data is None:
            # 조회 실패 — 직전 정상 캐시 유지(일시 에러로 포지션이 깜빡 사라지지 않게)
            if cached:
                return JSONResponse(cached)
            return JSONResponse([])
        # 조회 성공 — 빈 잔고([])라도 캐시를 갱신해 '판 종목이 계속 보이는' 폴백을 끊는다
        _POSITIONS_CACHE["data"] = data
        _POSITIONS_CACHE["at"] = now
        return JSONResponse(data)

    @app.post("/api/positions/refresh")
    def api_positions_refresh():
        """캐시 무시하고 KIS 에서 포지션 강제 재조회."""
        data = _live_positions()
        if data is None:
            # 조회 실패 — 캐시 유지, 직전 정상값 반환(없으면 빈 리스트)
            return JSONResponse(_POSITIONS_CACHE["data"] or [])
        # 성공 — 빈 잔고라도 캐시 갱신
        _POSITIONS_CACHE["data"] = data
        _POSITIONS_CACHE["at"] = time.time()
        return JSONResponse(data)

    @app.get("/params", response_class=HTMLResponse)
    def params_page(request: Request):
        template_path = Path(__file__).parent / "templates" / "params.html"
        _html = template_path.read_text(encoding="utf-8")
        resp = HTMLResponse(_html)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    @app.get("/charts", response_class=HTMLResponse)
    def charts_page():
        template_path = Path(__file__).parent / "templates" / "charts.html"
        resp = HTMLResponse(template_path.read_text(encoding="utf-8"))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    @app.get("/api/chart/symbols")
    def api_chart_symbols():
        """차트 탭 좌측 목록 — 스톡봇/대장주 그룹 + 각 그룹 봉 간격(env 설정 반영)."""
        stock_syms = [s.split(".")[0] for s in settings.symbols]
        stock_list = [{"code": s, "name": get_name(s)} for s in stock_syms]
        leader = _leader_today()
        seen: set[str] = set()
        leader_list: list[dict] = []
        for m in leader.get("basket", []):
            c = m.get("code")
            if not c or c in seen:
                continue
            seen.add(c)
            leader_list.append({"code": c, "name": m.get("name") or get_name(c), "tag": "바스켓"})
        for slot, tag in (("holding", "보유"), ("done", "완료")):
            d = leader.get(slot)
            c = (d.get("code") or d.get("symbol")) if d else None  # 상태 dict 는 symbol 키
            if c and c not in seen:
                seen.add(c)
                if d.get("virtual"):
                    tag += "(가상)"
                leader_list.append({"code": c, "name": d.get("name") or get_name(c), "tag": tag})
        return JSONResponse({
            "stock": {"interval_min": settings.live_candle_minutes, "symbols": stock_list},
            "leader": {"interval_min": settings.leader_interval_min, "symbols": leader_list},
        })

    @app.get("/api/chart/data/{code}")
    def api_chart_data(code: str):
        """봇이 떨군 분봉 스냅샷(data/charts/{code}.json) 반환 — KIS 호출 없음."""
        import json as _json
        safe = "".join(ch for ch in code.split(".")[0] if ch.isalnum())
        path = Path(__file__).resolve().parents[2] / "data" / "charts" / f"{safe}.json"
        if not path.exists():
            return JSONResponse({"symbol": safe, "bars": [], "missing": True})
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return JSONResponse({"symbol": safe, "bars": [], "error": str(e)})
        data["age_sec"] = int(time.time() - float(data.get("updated_at", 0) or 0))
        # 표준 등락율(전일종가 기준) 표시용 전일 종가 — 네이버 시세에서 유도
        # (전일종가 = 현재가 − 전일대비). KIS 호출 없음, 실패해도 차트는 그려짐.
        try:
            from stock_bot.broker import naver_quote
            q = naver_quote.fetch_quotes([safe]).get(safe)
            if q and q.get("price") is not None and q.get("change") is not None:
                data["prev_close"] = round(q["price"] - q["change"], 4)
        except Exception:
            pass
        return JSONResponse(data)

    @app.get("/api/overrides/raw")
    def api_overrides_raw():
        """.env.overrides 파일 원본 텍스트 반환 — PC 동기화용."""
        from fastapi.responses import PlainTextResponse
        override_path = ENV_PATH.parent / ".env.overrides"
        if not override_path.exists():
            return PlainTextResponse("", status_code=200)
        return PlainTextResponse(override_path.read_text(encoding="utf-8"))

    @app.get("/api/params")
    def api_get_params(request: Request):
        """.env → .env.overrides 순서로 읽어 파라미터 반환 (overrides 우선).

        읽기전용 토큰(X-Web-Token) 접근이면 시크릿 유출 방지를 위해
        ALLOWED_PARAM_KEYS(운영 파라미터 화이트리스트)만 반환한다. .env 에는
        KIS_APP_SECRET·ANTHROPIC_API_KEY·KRX_PW 등 시크릿이 들어있는데 이들은
        화이트리스트에 없으므로 자동 제외된다. 세션 로그인은 기존대로 전체 반환."""
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

        # .env/.env.overrides 어디에도 없는 허용 키는 settings 기본값으로 채운다.
        # (신규 파라미터가 두 파일에 아직 안 적혀 파라미터 탭에서 빈칸으로 뜨는 문제 방지)
        for key in ALLOWED_PARAM_KEYS:
            if key in result:
                continue
            v = getattr(settings, key.lower(), None)
            if v is None or isinstance(v, (list, dict, tuple, set)):
                continue  # 스칼라만 (SYMBOLS 등 컬렉션·프로퍼티는 별도 처리)
            result[key] = "true" if v is True else "false" if v is False else str(v)

        # 읽기전용 토큰 접근이면 운영 파라미터 화이트리스트만 (시크릿 제외)
        if getattr(request.state, "read_token", False):
            result = {k: v for k, v in result.items() if k in ALLOWED_PARAM_KEYS}
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
        "REGIME_BLOCK_ENABLED", "REGIME_MA_PERIOD", "REGIME_MOM_DAYS",
        "STOCK_DAILY_GATE_ENABLED", "STOCK_DAILY_GATE_MA",
        "STOCK_DAILY_GATE_SLOPE_DAYS", "STOCK_DAILY_GATE_SLOPE_PCT",
        "TAKE_PROFIT_ENABLED", "TAKE_PROFIT_PCT", "TAKE_PROFIT_FRACTION",
        "POSITION_SIZING", "POSITION_FRACTION", "ACCOUNT_SIZE_KRW",
        "DAILY_CONTEXT_PROFIT_GATE_PCT", "DAILY_CONTEXT_AVWAP_PCT",
        "DAILY_CONTEXT_PDH_PCT", "DAILY_CONTEXT_PDC_PCT",
        "DAILY_CONTEXT_TREND_BONUS",
        "OVERNIGHT_SELL_THRESHOLD", "OVERNIGHT_MIN_SELL_VOTES",
        "NEWS_PREFER_LLM", "NEWS_PAGES_PER_SYMBOL",
        "ENSEMBLE_NEWS_VETO_THRESHOLD", "ENSEMBLE_NEWS_STRONG_NEG_RATIO",
        "LLM_BACKEND",
        "PREMARKET_REVIEW_MODEL", "DAILY_REVIEW_MODEL", "NEWS_SENTIMENT_MODEL",
        "API_BUDGET_USD",
        "SELL_ON_NEXT_OPEN",
        "SYMBOLS",
        "SCREENER_SECTOR", "SCREENER_TOP_N",
        "SCREENER_AUTO_SECTOR", "SCREENER_DOWNTREND_HALVE",
        "SCREENER_LLM_REVIEW_ENABLED", "SCREENER_REVIEW_WEBSEARCH",
        "SCREENER_RS_DAYS", "SCREENER_MIN_STOCKS",
        "SCREENER_MARKET_TOP", "SCREENER_NAVER_RPM",
        "SCREENER_REMOTE_ENABLED",
        "SCREENER_MIN_LISTING_DAYS", "SCREENER_MIN_TURNOVER_EOK",
        "SCREENER_MIN_CAP_EOK",
        "TRADE_DRY_RUN",
        "LIVE_CANDLE_MINUTES",
        "LEADER_TRADE_ENABLED", "LEADER_BUDGET_KRW", "LEADER_INTERVAL_MIN",
        "LEADER_W", "LEADER_STOP_BUF_PCT", "LEADER_TP_PCT", "LEADER_ENTRY_MODE", "LEADER_VWAP_TOL",
        "LEADER_MAX_PULL_PCT", "LEADER_PHWIN_MIN", "LEADER_FIB_PCT", "LEADER_ANCHOR", "LEADER_ANCHOR_EMA",
        "LEADER_ANCHOR_TOL", "LEADER_VOLFILTER", "LEADER_FIB_DYNAMIC", "LEADER_RECLAIM", "LEADER_BAND_RATIO",
        "LEADER_SWITCH_ENABLED", "LEADER_SWITCH_INTERVAL_MIN", "LEADER_SWITCH_UNTIL",
        "LEADER_SWITCH_MOVE_MAX_PCT", "LEADER_MAX_SECTORS",
        "LEADER_BAR_RANGE_PCT", "LEADER_DAILY_TREND_GATE", "LEADER_CLOSE_TIME", "LEADER_OWN_SYMBOL_PRIORITY",
        "LEADER_SEL_TOP", "LEADER_SEL_RISE_MIN", "LEADER_SEL_HOT_MIN", "LEADER_SEL_VOL_MULT",
        "LEADER_SEL_MIN_VALUE_EOK", "LEADER_SEL_MIN_VALUE_ANCHOR_HHMM",
        "LEADER_SEL_MAX_VALUE_EOK", "LEADER_SEL_MIN_VALUE_FLOOR_EOK",
        "LEADER_SEL_SECTOR_TOP3", "LEADER_SEL_MIN_CAP_EOK", "LEADER_SEL_MAX_CHANGE",
        "LEADER_SEL_TURNOVER_CAP_PCT",
        "LEADER_MF_CLAMP_LOW", "LEADER_MF_CLAMP_HIGH",
        "STOCK_CAPITAL_KRW", "LEADER_CAPITAL_KRW", "INITIAL_CAPITAL_KRW",
        # 2026-08-11: 수급 제거 — LEAD_ST_W_FLOW + LEAD_ST_NF_W_* 삭제
        "LEAD_ST_W_VALUE", "LEAD_ST_W_UPDN", "LEAD_ST_W_TURNOVER", "LEAD_ST_W_SURGE",
        "LEAD_SC_W_INTENSITY", "LEAD_SC_W_BREADTH",
        "SCREENER_SECTOR_POS_RATIO_DOWN",
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
        # 전략별 자금 단일 파라미터 → 실제 거래자금/분모 자동 동기화
        #   · STOCK_CAPITAL_KRW(거래실행모드) → ACCOUNT_SIZE_KRW (스톡봇이 이 자본으로 거래)
        #   · LEADER_BUDGET_KRW(대장주탭)     → LEADER_CAPITAL_KRW (하루 1종목 전액 진입)
        #   · INITIAL_CAPITAL_KRW = 스톡봇 + 대장주 (총 수익률 분모)
        if {"STOCK_CAPITAL_KRW", "LEADER_CAPITAL_KRW", "LEADER_BUDGET_KRW"} & set(safe):
            def _f(v, default):
                try:
                    return float(str(v).replace(",", ""))
                except (TypeError, ValueError):
                    return float(default)
            # 🤖 스톡봇: 초기자금 = 거래 자본(ACCOUNT_SIZE_KRW)
            stock_cap = _f(safe.get("STOCK_CAPITAL_KRW"), settings.stock_capital_krw)
            if "STOCK_CAPITAL_KRW" in safe:
                safe["STOCK_CAPITAL_KRW"] = str(int(stock_cap))
                safe["ACCOUNT_SIZE_KRW"] = str(int(stock_cap))
            # 👑 대장주: 단일 파라미터(진입예산=초기자금). 어느 키로 들어와도 둘 다 동일값.
            if "LEADER_BUDGET_KRW" in safe:
                leader_cap = _f(safe["LEADER_BUDGET_KRW"], settings.leader_budget_krw)
            elif "LEADER_CAPITAL_KRW" in safe:
                leader_cap = _f(safe["LEADER_CAPITAL_KRW"], settings.leader_capital_krw)
            else:
                leader_cap = float(settings.leader_capital_krw)
            if "LEADER_BUDGET_KRW" in safe or "LEADER_CAPITAL_KRW" in safe:
                safe["LEADER_BUDGET_KRW"] = str(int(leader_cap))
                safe["LEADER_CAPITAL_KRW"] = str(int(leader_cap))
            safe["INITIAL_CAPITAL_KRW"] = str(int(stock_cap + leader_cap))
        # A안: 예산(충전액) 저장 시 리셋 시점을 자동 기록.
        # 이후 잔여 = 충전액 − (이 시점 이후 사용액). "0되면 재충전" 흐름과 일치.
        if "API_BUDGET_USD" in safe:
            import time as _t
            safe["API_BUDGET_RESET_AT"] = str(int(_t.time()))
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
        if "LIVE_CANDLE_MINUTES" in safe:
            try:
                _cm = int(safe["LIVE_CANDLE_MINUTES"])
                if _cm in (1, 3, 5, 10, 15, 30, 60):
                    settings.live_candle_minutes = _cm
            except (TypeError, ValueError):
                pass
        # 전략별 초기자금/거래자금 즉시 반영 (env 워처보다 빠르게)
        for _k, _attr in (
            ("STOCK_CAPITAL_KRW", "stock_capital_krw"),
            ("LEADER_CAPITAL_KRW", "leader_capital_krw"),
            ("INITIAL_CAPITAL_KRW", "initial_capital_krw"),
            ("ACCOUNT_SIZE_KRW", "account_size_krw"),
            ("LEADER_BUDGET_KRW", "leader_budget_krw"),
            ("API_BUDGET_USD", "api_budget_usd"),
            ("API_BUDGET_RESET_AT", "api_budget_reset_at"),
        ):
            if _k in safe:
                try:
                    setattr(settings, _attr, float(safe[_k]))
                except (TypeError, ValueError):
                    pass
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
        # 실전은 항상 네이버 테마 모드. mode 파라미터는 UI 하위호환용(무시).
        cmd = [sys.executable, str(script), "--once", "--ignore-hours", "--summary-only", "--theme"]
        # 선별 기준을 봇과 동일하게 주입(파라미터 탭에서 조정한 값 반영) — 선별 로직은
        # leader_finder 무변경, 임계값만 전달. 기본값=현행 하드코딩값이라 미변경 시 동작 불변.
        cmd += [
            "--top", str(int(settings.leader_sel_top)),
            "--rise-min", str(float(settings.leader_sel_rise_min)),
            "--hot-min", str(int(settings.leader_sel_hot_min)),
            "--vol-mult", str(float(settings.leader_sel_vol_mult)),
            "--min-value", str(float(settings.leader_sel_min_value_eok)),
            "--min-mktcap", str(float(settings.leader_sel_min_cap_eok)),
            "--max-change", str(float(settings.leader_sel_max_change)),
        ]
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
    def api_leader_job(job_id: str):
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
            "market_top": int(env.get("SCREENER_MARKET_TOP", "1000")),
            # ── 장전 자동 분석(장분석+섹터분석) ──────────────────────────
            "auto_sector":     _b(env.get("SCREENER_AUTO_SECTOR", "1")),   # 기본 ON
            "rs_days":         int(env.get("SCREENER_RS_DAYS",    "20")),
            "min_stocks":      int(env.get("SCREENER_MIN_STOCKS", "5")),
            "universe_top":    int(env.get("SCREENER_UNIVERSE_TOP", "200")),
            "downtrend_halve": _b(env.get("SCREENER_DOWNTREND_HALVE", "1")),
            # ── 장전 Claude 검수 (모델 = premarket_review.MODEL) ──
            "llm_review":      _b(env.get("SCREENER_LLM_REVIEW_ENABLED", "1")),  # 기본 ON
        }

    _SC_LOG_PATH = (
        Path("/app/data/screener_latest.log")
        if Path("/app/data").exists()
        else Path(__file__).resolve().parents[2] / "data" / "screener_latest.log"
    )

    # 날짜별 실행 로그 — git 추적 대상이라 백업 커밋에 포함, reset 에도 안 지워짐
    # (screener_latest.log 는 gitignore 런타임 파일이라 배포 reset 시 유실 가능)
    _SC_DAILY_DIR = _SC_LOG_PATH.parent / "screener"

    _SC_LOCK = threading.Lock()  # 스크리너 중복 실행 방지용 락

    # SSE 용 in-memory 스트림 버퍼 — 파일 I/O 의존 없음, append-only (cursor 기반 tail)
    _SC_STREAM_BUF: list[str] = []
    _SC_STREAM_MAX = 5000  # 최대 보관 줄 수

    # ── 원격(CI) 스코어링 오프로드 레지스트리 ────────────────────────────────
    # 파이 OOM 대책([[pi-oom-mitigation]] / [[krx-github-actions]]): 무거운 스코어링(③)을
    # GitHub Actions 로 넘긴다. 파이는 LAN-only(인바운드 불가)라 터널 없이 실시간 로그를
    # 받으려고 **GitHub Gist 중계**를 쓴다: 파이가 빈 gist 생성 → gist_id 를 workflow input
    # 으로 넘김 → CI(ci_screener_stream.py)가 screener.py stdout 을 그 gist 에 누적 PATCH →
    # 파이가 gist 를 ~2초 폴링해 새 내용을 로컬 실행과 동일한 consumer 로 흘린다(로그탭·
    # 저장 동형 보장). ①섹터선정·②검수·SYMBOLS는 파이 유지. 터널/도메인/포트개방 0.
    _SC_REMOTE_RUNS: dict[str, dict] = {}   # run_token -> {"consume","done","returncode","gist_id"}

    # 파이 gist 생성 헤더 — CI(ci_screener_stream.py _HEADER)와 **반드시 동일**해야 한다.
    #   내용이 항상 이 접두사로 시작 → 파이가 content[_consumed:] 증분만 안전 소비.
    #   (gist 폴백·터널 push 양쪽 공통. push 는 CI가 이 헤더로 시작하는 누적 전체를 POST.)
    _CI_GIST_HEADER = "[CI 스코어링 로그]\n"

    # cloudflared quick tunnel 이 기록한 현재 인바운드 URL(터널 실시간 push 용).
    #   파이 LAN-only라 CI→파이 인바운드는 이 터널로만 들어온다. 파일이 없으면(터널
    #   미가동) 자동으로 gist 폴백. 컨테이너는 /app/data, 로컬은 레포 data/.
    _TUNNEL_URL_FILE = (
        Path("/app/data/tunnel_url.txt")
        if Path("/app/data").exists()
        else Path(__file__).resolve().parents[2] / "data" / "tunnel_url.txt"
    )

    def _ci_callback_base() -> str | None:
        """CI가 로그를 push 할 파이 인바운드 base URL. 없으면 None(→ gist 폴백).

        우선순위: 수동 오버라이드(SCREENER_CI_CALLBACK_URL) → cloudflared 가 기록한
        tunnel_url.txt. quick tunnel 은 재시작마다 URL 이 바뀌므로 파이가 디스패치
        시점의 현재 URL 을 이 파일에서 읽어 workflow input 으로 실어보낸다.
        """
        import os as _o
        _u = (_o.environ.get("SCREENER_CI_CALLBACK_URL") or "").strip()
        if not _u:
            try:
                if _TUNNEL_URL_FILE.exists():
                    _u = _TUNNEL_URL_FILE.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                _u = ""
        _u = _u.rstrip("/")
        return _u if _u.startswith("http") else None

    def _remote_scoring_enabled() -> bool:
        """원격 스코어링 가능 여부 — 토글 ON + GitHub PAT 존재(없으면 로컬 폴백).

        토글(SCREENER_REMOTE_ENABLED)은 웹 파라미터 탭이 저장하는 .env.overrides 에서
        매 실행마다 라이브로 읽어 컨테이너 재기동 없이 즉시 반영한다(핫리로드는 settings
        객체만 갱신하고 os.environ 은 안 건드리므로). PAT(SCREENER_GH_TOKEN)은 시크릿이라
        .env(os.environ)에 두고 기동 시점 값을 쓴다. gist 중계라 터널 URL 은 불필요.
        (PAT 는 workflow 디스패치 + 중계 gist 생성/폴링/삭제에 쓰이며 gist 권한 필요.)
        """
        import os as _o
        _toggle = _o.environ.get("SCREENER_REMOTE_ENABLED", "")
        try:
            _ovp = ENV_PATH.parent / ".env.overrides"
            if _ovp.exists():
                for _ln in _ovp.read_text(encoding="utf-8", errors="replace").splitlines():
                    _ln = _ln.strip()
                    if _ln.startswith("SCREENER_REMOTE_ENABLED=") and not _ln.startswith("#"):
                        _toggle = _ln.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
        if _toggle.strip().lower() not in ("1", "true", "yes", "on"):
            return False
        return bool(_o.environ.get("SCREENER_GH_TOKEN"))

    def _gist_headers() -> dict:
        import os as _o
        return {
            "Authorization": f"Bearer {_o.environ.get('SCREENER_GH_TOKEN', '')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _gist_create(run_token: str) -> tuple[str, str]:
        """중계용 비공개 gist 생성 → (gist_id, raw_url) 반환. 실패 시 RuntimeError.

        내용은 CI(_HEADER)와 동일한 헤더로 시작해야 파이 증분 소비(content[_consumed:])가
        접두사 불변 가정을 지킨다.

        raw_url(gist.githubusercontent.com, SHA 미고정)도 함께 반환한다 — 폴링 읽기를
        api.github.com(5000/hr 사용자 한도에 카운트) 대신 이 raw 호스트로 하면 한도를
        전혀 안 먹는다(런당 폴링 ~180회를 통째로 한도 밖으로 뺌).
        """
        import requests as _rq
        payload = {
            "public": False,
            "description": f"screener-run relay · {run_token}",
            "files": {"screener.log": {"content": _CI_GIST_HEADER}},
        }
        try:
            r = _rq.post("https://api.github.com/gists", json=payload,
                         headers=_gist_headers(), timeout=30)
        except Exception as _e:   # noqa: BLE001
            raise RuntimeError(f"gist 생성 요청 실패: {_e}")
        if r.status_code != 201:
            raise RuntimeError(f"gist 생성 HTTP {r.status_code} {r.text[:160]}")
        _j = r.json() or {}
        _gid = _j.get("id")
        if not _gid:
            raise RuntimeError("gist 생성 응답에 id 없음")
        _owner = ((_j.get("owner") or {}).get("login")) or ""
        _raw = f"https://gist.githubusercontent.com/{_owner}/{_gid}/raw/screener.log"
        return str(_gid), _raw

    def _build_universe(market: str, market_top: int) -> str | None:
        """유니버스(종목·시총·이름)를 파이(한국 IP)에서 빌드해 JSON 문자열로 반환.
        screener.py --emit-universe 를 **subprocess** 로 돌려 pykrx 를 격리 — 종료 시
        메모리를 통째 반납해 파이 OOM 안전(무거운 스코어링은 여전히 CI 몫). KRX 조회가
        폴백(~40종목)으로 열화하면 SCREENER_FALLBACK_FATAL 가드가 rc≠0 로 실패시킨다.
        실패 시 None → 호출부가 원격 스코어링을 중단(빈 유니버스로 CI 낭비 방지)."""
        import os as _o, subprocess as _s, tempfile as _tf, json as _js
        _root = Path(__file__).resolve().parents[2]
        _sc = _root / "screener.py"
        _fd, _tmp = _tf.mkstemp(suffix=".json", prefix="uni_")
        _o.close(_fd)
        try:
            _env = _o.environ.copy()
            _env["PYTHONIOENCODING"] = "utf-8"
            _env["MALLOC_ARENA_MAX"] = "2"
            _env["SCREENER_FALLBACK_FATAL"] = "1"   # 파이 KRX 실패 시 폴백 채점 대신 실패
            _env.pop("SCREENER_UNIVERSE_URL", None)  # 파이 빌드는 항상 KRX 원천 조회
            # --mode 는 argparse required — emit 경로는 값과 무관하나(emit 후 즉시 return)
            # 없으면 argparse 가 exit 2 로 죽어 유니버스 빌드가 실패한다.
            _cmd = [sys.executable, str(_sc), "--mode", "weekly",
                    "--emit-universe", _tmp,
                    "--market", market, "--market-top", str(market_top)]
            _cwd = _root / "data"
            if not _cwd.exists():
                _cwd = _root
            _r = _s.run(_cmd, cwd=str(_cwd), env=_env, timeout=600,
                        stdout=_s.PIPE, stderr=_s.STDOUT, text=True,
                        encoding="utf-8", errors="replace")
            if _r.returncode != 0:
                logger.warning("유니버스 빌드 실패 rc={}: {}",
                               _r.returncode, (_r.stdout or "")[-300:])
                return None
            with open(_tmp, "r", encoding="utf-8") as _f:
                _content = _f.read()
            try:
                _rows = _js.loads(_content or "[]")
            except Exception:
                _rows = []
            if not _rows:
                logger.warning("유니버스 빌드 결과 비어있음 — 무시")
                return None
            return _content
        except _s.TimeoutExpired:
            logger.warning("유니버스 빌드 타임아웃(600s)")
            return None
        except Exception as _e:   # noqa: BLE001
            logger.warning("유니버스 빌드 예외: {}", _e)
            return None
        finally:
            try:
                _o.unlink(_tmp)
            except Exception:
                pass

    def _gist_create_universe(content: str, run_token: str) -> tuple[str, str]:
        """파이가 빌드한 유니버스 JSON 을 비공개 gist 로 올려 (gist_id, raw_url) 반환.
        CI(해외 IP)가 이 raw_url 을 받아 KRX 조회 없이 유니버스를 쓴다(option ①).
        실패 시 RuntimeError."""
        import requests as _rq
        payload = {
            "public": False,
            "description": f"screener-run universe · {run_token}",
            "files": {"universe.json": {"content": content}},
        }
        try:
            r = _rq.post("https://api.github.com/gists", json=payload,
                         headers=_gist_headers(), timeout=30)
        except Exception as _e:   # noqa: BLE001
            raise RuntimeError(f"유니버스 gist 생성 요청 실패: {_e}")
        if r.status_code != 201:
            raise RuntimeError(f"유니버스 gist 생성 HTTP {r.status_code} {r.text[:160]}")
        _j = r.json() or {}
        _gid = _j.get("id")
        if not _gid:
            raise RuntimeError("유니버스 gist 생성 응답에 id 없음")
        _owner = ((_j.get("owner") or {}).get("login")) or ""
        _raw = f"https://gist.githubusercontent.com/{_owner}/{_gid}/raw/universe.json"
        return str(_gid), _raw

    def _gist_read(gist_id: str) -> str | None:
        """gist 로그 파일 전체 내용 반환(폴링용). 실패/미존재 시 None.

        1MB 초과로 API 응답이 truncated 되면 raw_url 로 원문을 가져온다.
        """
        import requests as _rq
        try:
            r = _rq.get(f"https://api.github.com/gists/{gist_id}",
                        headers=_gist_headers(), timeout=20)
            if r.status_code != 200:
                return None
            _f = ((r.json() or {}).get("files") or {}).get("screener.log") or {}
            if _f.get("truncated") and _f.get("raw_url"):
                rr = _rq.get(_f["raw_url"], headers=_gist_headers(), timeout=30)
                return rr.text if rr.status_code == 200 else _f.get("content")
            return _f.get("content")
        except Exception:   # noqa: BLE001
            return None

    def _gist_read_raw(raw_url: str) -> str | None:
        """raw_url(gist.githubusercontent.com)로 로그 전문 조회 — API 5000/hr 한도 미소비.
        비공개 gist 도 전체 URL(추측불가)만 알면 무인증 접근 가능 → 인증헤더 미전송
        (토큰을 CDN 호스트로 흘리지 않음). CDN 캐시는 쿼리스트링 캐시버스터로 우회
        (측정상 PATCH 후 ~1초 내 최신 반영). raw 는 truncation 없이 항상 전문.
        실패 시 None → 호출부가 API(_gist_read)로 폴백하거나 다음 사이클 재시도.
        """
        import requests as _rq, time as _t
        try:
            r = _rq.get(f"{raw_url}?_={int(_t.time() * 1000)}", timeout=20)
            return r.text if r.status_code == 200 else None
        except Exception:   # noqa: BLE001
            return None

    def _gist_delete(gist_id: str) -> None:
        """중계 gist 정리(best effort)."""
        import requests as _rq
        try:
            _rq.delete(f"https://api.github.com/gists/{gist_id}",
                       headers=_gist_headers(), timeout=20)
        except Exception:   # noqa: BLE001
            pass

    def _dispatch_ci(sector: str, market: str, market_top: int, top_n: int,
                     workers: int, run_token: str, gist_id: str,
                     callback_url: str = "", universe_url: str = "") -> tuple[bool, str]:
        """screener-run.yml 을 workflow_dispatch 로 트리거. (성공여부, 메시지) 반환.

        callback_url 이 있으면 CI는 터널 push 모드(파이로 직접 POST), 없으면 gist_id
        로 gist PATCH 폴백. 둘 중 하나만 채워 보낸다.
        universe_url 은 파이(한국 IP)가 빌드해 gist 에 올린 유니버스 raw URL — CI 가
        이걸 받으면 KRX 조회를 건너뛴다(해외 IP 차단 회피, option ①).
        """
        import os as _o
        import requests as _rq
        repo = _o.environ.get("SCREENER_CI_REPO", "inwhan987/-")
        wf   = _o.environ.get("SCREENER_CI_WORKFLOW", "screener-run.yml")
        ref  = _o.environ.get("SCREENER_CI_REF", "main")
        url  = f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches"
        payload = {"ref": ref, "inputs": {
            "sector": sector or "", "market": market,
            "market_top": str(market_top), "top_n": str(top_n),
            "workers": str(workers), "gist_id": gist_id or "",
            "callback_url": callback_url or "", "universe_url": universe_url or "",
            "run_token": run_token,
        }}
        try:
            r = _rq.post(url, json=payload, timeout=30, headers=_gist_headers())
        except Exception as _e:   # noqa: BLE001
            return False, f"요청 실패: {_e}"
        if r.status_code == 204:
            return True, "dispatched"
        return False, f"HTTP {r.status_code} {r.text[:200]}"

    def _cancel_ci(run_token: str) -> tuple[bool, str]:
        """원격 CI 런 취소 — runs 목록에서 run-name 에 심긴 run_token 으로 매칭 후 cancel.

        디스패치 직후엔 런이 아직 목록에 안 뜰 수 있어(수초 지연) 못 찾을 수 있는데,
        그 경우엔 파이 쪽 폴링만 멈추고(로그탭 즉시 취소 표시) CI는 스스로 완료된다
        (파이가 gist 폴링을 중단할 뿐, 러너는 스코어링만 마저 돌고 종료, 무해).
        """
        import os as _o
        import requests as _rq
        repo = _o.environ.get("SCREENER_CI_REPO", "inwhan987/-")
        token = _o.environ.get("SCREENER_GH_TOKEN", "")
        if not token:
            return False, "SCREENER_GH_TOKEN 없음"
        hdr = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            lr = _rq.get(
                f"https://api.github.com/repos/{repo}/actions/runs",
                params={"event": "workflow_dispatch", "per_page": "30"},
                headers=hdr, timeout=20,
            )
            if lr.status_code != 200:
                return False, f"runs 조회 HTTP {lr.status_code}"
            runs = lr.json().get("workflow_runs", []) or []
            target = next(
                (w for w in runs
                 if run_token in (w.get("name") or "")
                 and w.get("status") in ("queued", "in_progress", "requested", "waiting")),
                None,
            )
            if not target:
                return False, "실행 런 미발견(아직 미등록이거나 이미 종료)"
            rid = target["id"]
            cr = _rq.post(
                f"https://api.github.com/repos/{repo}/actions/runs/{rid}/cancel",
                headers=hdr, timeout=20,
            )
            if cr.status_code == 202:
                return True, f"CI 런 {rid} 취소 요청됨"
            return False, f"cancel HTTP {cr.status_code} {cr.text[:120]}"
        except Exception as _e:   # noqa: BLE001
            return False, f"요청 실패: {_e}"

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

        # ── 로그 준비 — 장전 분석 출력도 로그탭(SSE)·날짜별 파일에 남도록 먼저 셋업 ──
        try:
            _SC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SC_DAILY_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as _mk_err:
            logger.warning("스크리너 로그 디렉터리 생성 실패: {}", _mk_err)
        _daily_log = _SC_DAILY_DIR / f"{datetime.now(tz=_KST).strftime('%Y-%m-%d')}.log"

        def _file_append(_text: str) -> None:
            try:
                with _SC_LOG_PATH.open("a", encoding="utf-8", errors="replace") as _lf:
                    _lf.write(_text)
                with _daily_log.open("a", encoding="utf-8", errors="replace") as _df:
                    _df.write(_text)
            except Exception as _write_err:
                logger.warning("스크리너 로그 파일 쓰기 실패: {}", _write_err)

        def _log_both(_text: str) -> None:
            # SSE 스트림 버퍼 + 파일 동시 기록 (여러 줄 입력 지원)
            for _ln in _text.splitlines():
                _SC_STREAM_BUF.append(_ln)
            _file_append(_text if _text.endswith("\n") else _text + "\n")

        # 로컬/원격 공통 라인 소비기 — screener.py stdout 한 줄을 로컬 실행과 100% 동일하게
        # 처리: _captured(파싱용, 개행 포함) + _SC_STREAM_BUF(SSE) + _file_append(날짜별 파일).
        # SCREENER_JSON_BEGIN~END 기계 페이로드는 파싱용으로만 보관하고 SSE/파일에선 제외.
        def _make_consumer(_captured: list):
            _st = {"in_json": False}
            def _consume(_raw: str) -> None:
                _line = _raw if _raw.endswith("\n") else _raw + "\n"
                _captured.append(_line)
                _s = _line.strip()
                if "SCREENER_JSON_BEGIN" in _s:
                    _st["in_json"] = True
                    return
                if "SCREENER_JSON_END" in _s:
                    _st["in_json"] = False
                    return
                if _st["in_json"]:
                    return
                _SC_STREAM_BUF.append(_line.rstrip())
                _file_append(_line)
            return _consume

        if len(_SC_STREAM_BUF) > _SC_STREAM_MAX:
            del _SC_STREAM_BUF[:-_SC_STREAM_MAX]  # 오래된 줄 정리 (최근 5000줄 유지)
        _run_t0 = _time.time()   # 총 소요시간 측정 시작 (완료/오류 알림에 표기)
        _log_both(f"━━━ 새 스크리너 실행  {_time.strftime('%Y-%m-%d %H:%M:%S')} ━━━")

        def _elapsed_str() -> str:
            _sec = int(_time.time() - _run_t0)
            _m, _s = divmod(_sec, 60)
            return f"{_m}분 {_s}초" if _m else f"{_s}초"

        # ── 취소 플래그 — 어느 단계(장전분석/스코어링/검수)에서 눌러도 즉시 중단 ──
        #   /cancel 엔드포인트가 _SC_JOBS[job_id]["cancelled"]=True 로 세팅한다.
        #   각 단계 경계에서 이 플래그를 확인하고 True 면 뒷 단계를 타지 않고 빠져나온다.
        #   (실행 중인 subprocess 는 /cancel 이 pkill 로 이미 죽였고, 여기선 파이썬
        #    스레드가 다음 단계로 진입하는 걸 막는다 — pkill 후 새 screener 재기동 방지.)
        def _cancelled() -> bool:
            return bool((_SC_JOBS.get(job_id) or {}).get("cancelled"))

        def _abort_if_cancelled(_stage: str) -> bool:
            if not _cancelled():
                return False
            _log_both(f"━━━ 스크리너 취소됨 ({_stage}) · ⏱ {_elapsed_str()} ━━━")
            _SC_JOBS[job_id].update({"status": "error", "output": "사용자 취소"})
            try:
                notify(f"⏹ 스크리너 취소됨 ({_stage}) · ⏱ {_elapsed_str()}")
            except Exception:
                pass
            return True

        # ── 장전 자동 분석: 자동 트리거 + SCREENER_AUTO_SECTOR ON 일 때만 ──────
        sc_market = "all"   # 기본: 코스피+코스닥 1600 (섹터 선정 여부와 무관하게 항상 all)
        _analysis_note = ""   # 장전 분석 요약 — 스크리너 완료 알림에 합쳐 1회만 전송
        _reg: dict = {}       # 레짐 (장전 분석 성공 시 채워짐 — 종목 검수에서도 사용)
        _sector_review_line = ""  # 섹터 검수 결과 (Discord 알림용)
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
                    _ma_env["PYTHONUNBUFFERED"] = "1"
                    _log_both("[장전 분석 시작 — 장세 판정 + 섹터 강도 분석 (최대 10분)]")
                    # 진행 과정 실시간 표시: run(capture) 대신 Popen + 라인 스트리밍
                    _ma_p = _sp.Popen(
                        _ma_cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=_ma_env, cwd=str(_ma_root), bufsize=1,
                    )
                    # /cancel 이 pkill 없이도 이 프로세스를 직접 죽일 수 있게 핸들 등록.
                    (_SC_JOBS.get(job_id) or {})["proc"] = _ma_p
                    _ma_lines: list[str] = []

                    def _ma_reader():
                        try:
                            for _l in _ma_p.stdout:
                                _ma_lines.append(_l)
                                _s = _l.strip()
                                # JSON 페이로드/마커는 로그탭 노이즈 → 스트리밍 제외 (파싱용으로만 보관)
                                if "ANALYSIS_JSON" in _s or (_s.startswith("{") and _s.endswith("}")):
                                    continue
                                _log_both(_l.rstrip())
                        except Exception as _ma_re:
                            logger.warning("장전 분석 PIPE 읽기 오류: {}", _ma_re)

                    _ma_rt = threading.Thread(target=_ma_reader, daemon=True)
                    _ma_rt.start()
                    try:
                        _ma_p.wait(timeout=600)  # 코스피200+코스닥200 → 넉넉히 10분
                    except _sp.TimeoutExpired:
                        _ma_p.kill()
                        _ma_p.wait()
                        raise
                    _ma_rt.join(timeout=10)
                    _ma_out = "".join(_ma_lines)
                    _ma_err = ""  # stderr 는 stdout 에 합류 → 오류 상세도 _ma_out 에 포함
                    if "ANALYSIS_JSON_BEGIN" not in _ma_out:
                        # subprocess가 마커 없이 종료 → stderr에 실제 traceback 있음
                        _err_detail = (_ma_err or _ma_out or "(출력 없음)")[-400:]
                        raise RuntimeError(f"분석 subprocess 출력 파싱 실패: {_err_detail}")
                    _j = _ma_out.split("ANALYSIS_JSON_BEGIN", 1)[1]
                    _j = _j.split("ANALYSIS_JSON_END", 1)[0]
                    # BEGIN~END 사이에 analyze() 진행로그(leader_finder/screener/pykrx)가
                    # 섞일 수 있으므로 '{'로 시작·'}'로 끝나는 JSON 한 줄만 골라 파싱한다.
                    # (그냥 strip 후 loads 하면 로그가 앞에 끼어 "Expecting value char 0" 발생)
                    _cand = ""
                    for _line in _j.splitlines():
                        _s = _line.strip()
                        if _s.startswith("{") and _s.endswith("}"):
                            _cand = _s
                    if not _cand:
                        # JSON 본문 없음 → 프로세스가 analyze() 도중 강제 종료됨
                        _err_detail = (_ma_err or _j.strip() or "(stderr 없음)")[-400:]
                        raise RuntimeError(f"분석 프로세스가 도중 강제 종료됨: {_err_detail}")
                    _res = _json.loads(_cand)
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
                    # ── 장전 Claude 검수 ① 섹터 ──────────────────────────────
                    # 알고리즘 최강 섹터를 레드팀 검수 → 부적합 시 랭킹 내 차순위
                    # eligible 섹터로 자동 전환. 실패·무효 시 알고리즘 유지(fail-safe).
                    if _acfg.get("llm_review") and _ts:
                        try:
                            from stock_bot.live.premarket_review import review_sector, model_label
                            _mdl = model_label()   # 표기 = MODEL 상수 자동 추종
                            _sr = review_sector(_reg, _res["ranking"], _ts)
                            if _sr.get("ok") and _sr.get("decision") == "switch":
                                _new_sec = _sr["chosen_sector"]
                                _cost_tag = f" · ${_sr['cost_usd']:.4f}" if _sr.get("cost_usd") else ""
                                _sector_review_line = (
                                    f"🔬 **섹터 검수**({_mdl}) · {_ts} → **{_new_sec}** 전환{_cost_tag}"
                                    + (f"\n  └ {_sr.get('reason','')}" if _sr.get("reason") else "")
                                )
                                _ts = _new_sec
                                sector = _new_sec
                                sc_market = "all"
                                try:
                                    _ov = ENV_PATH.parent / ".env.overrides"
                                    _txt = _ov.read_text(encoding="utf-8") if _ov.exists() else ""
                                    _new, _n = _re.subn(r"^(SCREENER_SECTOR\s*=).*$",
                                                        f"SCREENER_SECTOR={_new_sec}",
                                                        _txt, flags=_re.MULTILINE)
                                    _ov.write_text(
                                        _new if _n > 0 else _txt.rstrip() + f"\nSCREENER_SECTOR={_new_sec}\n",
                                        encoding="utf-8",
                                    )
                                except Exception as _e3:
                                    logger.warning("검수 섹터 반영 실패: {}", _e3)
                            elif _sr.get("ok"):
                                _cost_tag = f" · ${_sr['cost_usd']:.4f}" if _sr.get("cost_usd") else ""
                                _sector_review_line = (
                                    f"🔬 **섹터 검수**({_mdl}) · {_ts} 유지{_cost_tag}"
                                    + (f"\n  └ {_sr.get('reason','')}" if _sr.get("reason") else "")
                                )
                        except Exception as _e3:
                            logger.warning("섹터 검수 실패 — 알고리즘 유지: {}", _e3)
                    if _reg["regime"] == "down" and _acfg["downtrend_halve"]:
                        top_n = max(1, top_n // 2)   # 하락장 → 종목 수 절반
                    _rk = _res["ranking"][:5]
                    # med_rs = 중앙값(섹터 강도 기준), pos_ratio = 상승종목 비율
                    # 세로 랭킹 리스트 — 최종 선정 섹터(_ts, 검수 전환 반영)에 ✅ 표시
                    _rk_lines = []
                    for _i, s in enumerate(_rk, 1):
                        _sel_tag = "  ✅ 선정" if _ts and s["sector"] == _ts else ""
                        _rk_lines.append(
                            f"  {_i}. {s['sector']}  "
                            f"{s.get('med_rs', s['avg_rs']):+.1f}% · "
                            f"상승 {s.get('pos_ratio', 0) * 100:.0f}% ({s['count']}종목){_sel_tag}"
                        )
                    _rk_str = "\n".join(_rk_lines) or "  (없음)"
                    # 선정 섹터가 왜 상위인지 — 구성종목(RS 상위) 펼쳐 보이기
                    _mem_str = ""
                    if _ts:
                        _sel_row = next(
                            (s for s in _res["ranking"] if s["sector"] == _ts), None)
                        _mem = (_sel_row or {}).get("members") or []
                        if _mem:
                            _mem_parts = [
                                f"{get_name(str(c)) or c}({str(c)}) {float(r):+.0f}%"
                                for c, r in _mem[:8]
                            ]
                            _mem_str = (
                                f"\n\n📌 **{_ts} 강도 근거** (RS 상위 {len(_mem_parts)}종목)\n"
                                f"  {' · '.join(_mem_parts)}"
                            )
                    _ks = _reg.get("kospi") or {}
                    _kq = _reg.get("kosdaq") or {}
                    _idx_str = (
                        f"코스피 {_ks.get('gap_pct', 0):+.1f}% · "
                        f"코스닥 {_kq.get('gap_pct', 0):+.1f}%"
                    )
                    _uni = _res.get("universe") or {}
                    _uni_str = ""
                    if _uni:
                        _uni_str = (
                            f"{_uni.get('src', '?')} {_uni.get('size', '?')}종목"
                            f"{' · KRX미로그인→네이버폴백' if _uni.get('src') == 'naver' else ''}"
                        )
                    # 순서: ① 장세  ② 섹터 랭킹(선정표시)  ③ 검수  — 랭킹부터 보이게
                    _analysis_note = (
                        f"🔎 **장전 분석** · {_reg_kr}  "
                        f"(지수평균 {_reg['gap_pct']:+.1f}% vs 20일선 · {_idx_str})\n\n"
                        f"🏅 **섹터 강도 TOP5**"
                        f"  ({(_uni_str + ' · ') if _uni_str else ''}20일수익률 중앙값)\n"
                        f"{_rk_str}"
                        f"{_mem_str}"
                    )
                    if not _ts:
                        _analysis_note += "\n  ⚠️ 선정 섹터 없음 (상승비율 50% 충족 섹터 없음 → 기본 유지)"
                    if _sector_review_line:
                        _analysis_note += f"\n\n{_sector_review_line}"
                    logger.info("장전 분석 완료: regime={} top_sector={} top_n={} market={}",
                                _reg["regime"], _ts, top_n, sc_market)
                except _sp.TimeoutExpired:
                    # 10분 초과 → 자식 프로세스 종료됨, 기본 설정값으로 폴백
                    logger.warning("장전 분석 타임아웃(600s) — 기본 설정으로 진행")
                    _analysis_note = "🔎 **장전 분석** — ⚠️ 타임아웃(10분 초과) → 기본 설정으로 진행"
                except Exception as _e:
                    logger.warning("장전 분석 실패 — 기본 설정으로 진행: {}", _e)
                    _analysis_note = f"🔎 **장전 분석** — ⚠️ 실패({_e}) → 기본 설정으로 진행"

        # 장전 분석 요약(장세·선정 섹터·TOP5)도 로그탭 + 날짜별 파일에 기록
        if _analysis_note:
            _log_both(_analysis_note)

        # 장전분석 단계에서 취소됐으면 스코어링으로 넘어가지 않고 즉시 종료.
        #   (pkill 로 market_analysis.py 는 이미 죽었고, 여기서 screener 재기동을 막는다.)
        if _abort_if_cancelled("장전분석 후"):
            return

        root = Path(__file__).resolve().parents[2]
        sc_script = root / "screener.py"
        # market_top: 코스피/코스닥 각 시총 상위 N (0=전체). SCREENER_MARKET_TOP 로 조절.
        # 과거엔 섹터 지정 시 200으로 강제했으나, 유니버스 확대 요청으로 설정값을 그대로 사용.
        # (넓힐수록 네이버 요청 급증 → screener.py 전역 rate limiter가 차단 방지)
        effective_market_top = market_top
        effective_workers    = 1   if sector else 2   # 2→1/4→2 (2026-07-06): 파이 650m 캡.
        #   단일 스레드면 malloc_trim이 힙 꼭대기를 실제 반납 → RSS creep 억제.
        #   속도는 네이버 80RPM 전역 스로틀이 병목이라 워커 축소해도 벽시계 시간 거의 동일.
        # 원격(CI)은 7GB 러너라 OOM 무관 → 위 파이 캡을 씌우면 정반대(워커1 → 1600종목
        #   직렬 ~100분 → 30분 타임아웃 초과 + 한 종목 hang 시 전체 정지). CI엔 넉넉히.
        remote_workers = 8
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
        # glibc arena 개수 제한(스레드 워커 단편화 RSS 억제) — 스크리너 subprocess에만 주입.
        #   파이 650m 캡 내 1000종목 완주 여지 확보. 웹 본체엔 무영향(여기 env는 이 Popen 전용).
        #   screener.py 의 주기적 malloc_trim 과 병행.
        env["MALLOC_ARENA_MAX"] = "2"

        # 서브프로세스 CWD: /app(이미지 루트, root 소유·uid1000 쓰기불가) 대신
        # 바인드 마운트된 쓰기가능 data/ 로. OpenDartReader v0.3.2 가 상대경로
        # 'docs_cache/' 를 CWD 에 makedirs 하는데 /app 은 root 소유라 Permission
        # denied 가 났음. screener 자체 파일은 전부 HERE(절대경로)라 cwd 무관.
        _sc_cwd = root / "data"
        try:
            _sc_cwd.mkdir(exist_ok=True)
        except Exception:
            _sc_cwd = root

        try:
            # 로컬/원격 공통 파싱 버퍼 + 라인 소비기 (저장·SSE 동형 보장의 단일 지점)
            _captured_lines: list[str] = []
            _consume = _make_consumer(_captured_lines)

            if _remote_scoring_enabled():
                # ── 원격(CI) 스코어링 — 무거운 ③ 스코어링만 GitHub Actions 로 오프로드 ──
                #    두 전송 방식(하나는 CI가, 하나는 파이가 주도) — 소비 로직은 동일:
                #    CI가 항상 헤더로 시작하는 **누적 전체 로그**를 흘리고, 파이는
                #    content[_consumed:] 증분만 _consume(로컬과 동일 처리)한다.
                #    (A) 터널 push(기본): cloudflared quick tunnel 이 뚫려 있으면 CI가
                #        파이 /api/screener/ingest 로 누적 전체를 직접 POST → ingest 가
                #        메모리 버퍼 갱신 + wake → 실시간, GitHub API 한도 무관.
                #    (B) gist 폴백: 터널 URL 이 없으면 CI가 gist 에 PATCH → 파이가 raw_url
                #        폴링(API 한도 미소비). 터널 없이도 동작 보장.
                import time as _t2
                _run_token = uuid.uuid4().hex
                # ── 유니버스 핸드오프(option ①) — 파이(한국 IP)가 KRX 유니버스를 빌드해
                #    gist 로 올리고 CI 엔 raw_url 만 넘긴다. CI(해외 IP)는 KRX 를 건드리지
                #    않아 간헐적 빈-응답 차단을 근본 회피. subprocess 격리로 파이 OOM 안전.
                _uni_gid = None
                _uni_url = ""
                _SC_STREAM_BUF.append("[유니버스 빌드 중(파이 한국 IP, KRX 조회)... 약 30~90초]")
                _uni_content = _build_universe(sc_market, effective_market_top)
                if not _uni_content:
                    raise RuntimeError(
                        "유니버스 빌드 실패(파이 KRX 조회) — 원격 스코어링 중단")
                _uni_gid, _uni_url = _gist_create_universe(_uni_content, _run_token)
                try:
                    import json as _juni
                    _ucnt = len(_juni.loads(_uni_content) or [])
                except Exception:
                    _ucnt = 0
                _SC_STREAM_BUF.append(f"[유니버스 {_ucnt}종목 빌드 완료 → CI 로 핸드오프]")
                _done = threading.Event()
                _cb_base = _ci_callback_base()
                _push_mode = bool(_cb_base)
                _gid = None
                _raw_url = None
                _run_rec = {
                    "consume": _consume, "done": _done, "returncode": None,
                    "job_id": job_id, "cancelled": False,
                }
                if _push_mode:
                    _run_rec["content"] = _CI_GIST_HEADER   # 헤더로 시작(증분 소비 접두사 불변)
                    _run_rec["wake"] = threading.Event()    # ingest 가 도착 즉시 폴링 루프를 깸
                    _SC_REMOTE_RUNS[_run_token] = _run_rec
                    _SC_STREAM_BUF.append("[CI 디스패치(터널 실시간) → 러너 부팅·패키지 로딩 약 30~60초]")
                    _ok, _msg = _dispatch_ci(
                        sector, sc_market, effective_market_top, top_n,
                        remote_workers, _run_token, "", _cb_base, _uni_url)
                    if not _ok:
                        _SC_REMOTE_RUNS.pop(_run_token, None)
                        if _uni_gid:
                            _gist_delete(_uni_gid)
                        raise RuntimeError(f"CI 디스패치 실패 — {_msg}")
                else:
                    _gid, _raw_url = _gist_create(_run_token)  # 실패 시 RuntimeError → 아래 except (job error)
                    _run_rec["gist_id"] = _gid
                    _SC_REMOTE_RUNS[_run_token] = _run_rec
                    _SC_STREAM_BUF.append("[CI 디스패치(gist 폴백) → 러너 부팅·패키지 로딩 약 30~60초]")
                    _ok, _msg = _dispatch_ci(
                        sector, sc_market, effective_market_top, top_n,
                        remote_workers, _run_token, _gid, "", _uni_url)
                    if not _ok:
                        _SC_REMOTE_RUNS.pop(_run_token, None)
                        _gist_delete(_gid)
                        if _uni_gid:
                            _gist_delete(_uni_gid)
                        raise RuntimeError(f"CI 디스패치 실패 — {_msg}")
                logger.info("스크리너 CI 디스패치 [{}]: token={} mode={} sector={} market={}",
                            job_id, _run_token, "push" if _push_mode else "gist", sector, sc_market)
                # ── 누적 로그 소비 → _consume (120분 상한, 취소 시 _done 로 즉시 깸) ──
                #    내용은 단조 증가 → content[_consumed:] 증분만 소비. 종료는 CI가 붙인
                #    __SCREENER_CI_DONE__ rc=<n> 센티넬을 파싱해 감지(직전 내용까지 모두
                #    소비한 뒤라 결과 JSON 유실 없음). 상한은 잡 timeout-minutes(120)과 동기화.
                _run_ref = _SC_REMOTE_RUNS[_run_token]
                _deadline = _t2.time() + 7200
                _consumed = 0
                _buf = ""
                _rc = None
                while _t2.time() < _deadline:
                    if _done.is_set() or _run_ref.get("cancelled"):
                        break
                    if _push_mode:
                        _content = _run_ref.get("content")   # ingest 가 갱신한 메모리 버퍼
                    else:
                        # 폴링 읽기는 raw_url(한도 미소비) 우선, 실패 시에만 API 폴백.
                        _content = _gist_read_raw(_raw_url)
                        if _content is None:
                            _content = _gist_read(_gid)   # raw 실패 시 API 폴백(드묾·한도 소비)
                    if _content is not None and len(_content) > _consumed:
                        _buf += _content[_consumed:]
                        _consumed = len(_content)
                        while "\n" in _buf:
                            _line, _buf = _buf.split("\n", 1)
                            if _line.startswith("__SCREENER_CI_DONE__"):
                                _mrc = _re.search(r"rc=(-?\d+)", _line)
                                _rc = int(_mrc.group(1)) if _mrc else 0
                                _run_ref["returncode"] = _rc
                                _done.set()
                                break
                            if _line == _CI_GIST_HEADER.rstrip("\n"):
                                continue   # 헤더 라인은 표시 생략
                            try:
                                _consume(_line)
                            except Exception as _ce:
                                logger.warning("CI consume 오류: {}", _ce)
                        if _done.is_set():
                            break
                    if _push_mode:
                        # ingest 도착 즉시 깸(실시간). 15초는 안전 하트비트(취소/유휴 대비).
                        _wk = _run_ref.get("wake")
                        if _wk is not None:
                            _wk.wait(timeout=15.0)
                            _wk.clear()
                        else:
                            _done.wait(timeout=1.0)
                    else:
                        _done.wait(timeout=10.0)  # 취소 시 즉시 깸, 아니면 10초 폴링 간격
                _run_info = _SC_REMOTE_RUNS.pop(_run_token, None) or {}
                if _gid:
                    _gist_delete(_gid)
                if _uni_gid:
                    _gist_delete(_uni_gid)
                if _run_info.get("cancelled"):
                    # 취소 엔드포인트가 폴링을 깨웠다 → 부분 출력 파싱 없이 종료
                    _SC_JOBS[job_id].update({"status": "error", "output": "사용자 취소(원격)"})
                    _log_both(f"━━━ 스크리너 취소됨 (원격 CI) · ⏱ {_elapsed_str()} ━━━")
                    notify(f"⏹ 스크리너 취소됨 (원격 CI) · ⏱ {_elapsed_str()}")
                    return
                if _rc is None:
                    _SC_JOBS[job_id].update({"status": "error", "output": "CI 타임아웃 (7200초 초과)"})
                    notify("⚠️ 스크리너 CI 타임아웃(120분) — 러너/gist 상태 확인 필요")
                    return
                _rc = int(_run_info.get("returncode") or _rc or 0)
            else:
                # ── 로컬 실행(기존 경로) ──
                _SC_STREAM_BUF.append("[시작 중... 패키지 로딩 약 30~60초 소요]")
                proc = _sp.Popen(
                    cmd,
                    stdout=_sp.PIPE, stderr=_sp.STDOUT,
                    cwd=str(_sc_cwd), env=env,
                    bufsize=1, text=True, encoding="utf-8", errors="replace",
                )
                # /cancel 이 직접 죽일 수 있게 핸들 등록(장전분석 핸들을 스코어링으로 교체).
                (_SC_JOBS.get(job_id) or {})["proc"] = proc

                # Reader thread: PIPE 한 줄씩 → 공통 _consume (파싱용+SSE+파일, JSON 블록 필터 포함)
                def _pipe_reader():
                    try:
                        for _line in proc.stdout:
                            _consume(_line)
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
                _rc = proc.returncode

            # 진단: 종료 코드 + 캡쳐 크기 로깅 (로컬/원격 공통)
            output = "".join(_captured_lines) or "(출력 없음)"
            logger.info(
                "스크리너 종료 [{}]: exit_code={} captured_lines={} captured_chars={}",
                job_id, _rc, len(_captured_lines), len(output)
            )
            # 스코어링 단계에서 취소됐으면 종목검수·SYMBOLS 갱신을 타지 않고 즉시 종료.
            #   (pkill 로 screener.py 는 이미 죽어 output 이 불완전 → 부분 결과 반영 방지.)
            if _abort_if_cancelled("스코어링 후"):
                return

            # 장전 분석 요약 + 스크리너 결과를 한 메시지로 합쳐 전송
            _sep = "\n────────────\n"
            _note_prefix = (_analysis_note + _sep) if _analysis_note else ""
            # "선별 N개: A,B,C" 파싱 → SYMBOLS 자동 업데이트 (dry run이면 스킵)
            m = _re.search(r"선별\s*\d+개:\s*((?:[A-Z0-9]+\.K[SQ](?:,\s*)?)+)", output)
            if m:
                # suffix 제거 후 6자리 코드로 통일 (000660.KS → 000660)
                _sel = [s.split(".")[0] for s in m.group(1).replace(" ", "").split(",") if s]
                # ── 장전 Claude 검수 ② 종목 ──────────────────────────────────
                # 스크리너 선별 종목을 레드팀 검수 → 레드플래그 시 벤치(차순위)에서
                # 교체. 실패·무효 시 알고리즘 선별 유지(fail-safe).
                _stock_review_line = ""
                if _acfg.get("llm_review"):
                    try:
                        _ranked = []
                        _mj = _re.search(r"SCREENER_JSON_BEGIN\s*(.+?)\s*SCREENER_JSON_END",
                                         output, _re.DOTALL)
                        if _mj:
                            _ranked = (_json.loads(_mj.group(1).strip()) or {}).get("ranked", [])
                        if _ranked and _sel:
                            from stock_bot.live.premarket_review import review_stocks, model_label
                            _mdl2 = model_label()   # 표기 = MODEL 상수 자동 추종
                            _rv = review_stocks(_reg, sector, _ranked, len(_sel), _sel)
                            _r_cost = f" · ${_rv['cost_usd']:.4f}" if _rv.get("cost_usd") else ""
                            if _rv.get("ok") and _rv.get("decision") == "swap":
                                _fin = _rv["final_symbols"]
                                _o = " · ".join(f"{get_name(c) or c}({c})" for c in _sel)
                                _n = " · ".join(f"{get_name(c) or c}({c})" for c in _fin)
                                _log_both(f"🔬 종목 검수({_mdl2}) · {_o} → {_n} 교체{_r_cost}")
                                _reason = _rv.get("reason", "") or ""
                                if _reason:
                                    _log_both(f"  └ {_reason}")
                                _stock_review_line = f"🔬 종목 검수({_mdl2}): {_o} → {_n} 교체됨"
                                if _reason:
                                    _stock_review_line += f"\n  └ {_reason}"
                                _sel = _fin
                            elif _rv.get("ok"):
                                _keep = " · ".join(f"{get_name(c) or c}({c})" for c in _sel)
                                _log_both(f"🔬 종목 검수({_mdl2}) · {_keep} 유지{_r_cost}")
                                _reason = _rv.get("reason", "") or ""
                                if _reason:
                                    _log_both(f"  └ {_reason}")
                                _stock_review_line = f"🔬 종목 검수({_mdl2}): {_keep} 유지"
                                if _reason:
                                    _stock_review_line += f"\n  └ {_reason}"
                    except Exception as _rev_e:
                        logger.warning("종목 검수 실패 — 알고리즘 유지: {}", _rev_e)
                symbols = ",".join(_sel)
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
                        + (f"{_stock_review_line}\n" if _stock_review_line else "")
                        + f"(검증모드 — 운용 종목 미반영) · ⏱ {_elapsed_str()}"
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
                        + (f"{_stock_review_line}\n" if _stock_review_line else "")
                        + f"운용 종목 자동 업데이트 완료 · ⏱ {_elapsed_str()}"
                    )
            else:
                notify(f"{_note_prefix}📊 스크리너 완료 — 매칭 종목 없음 (섹터: {sector or '전체'}) · ⏱ {_elapsed_str()}")
            _log_both(f"━━━ 스크리너 종료 · 총 소요 {_elapsed_str()} ━━━")
            _SC_JOBS[job_id].update({"status": "done", "output": output})
        except Exception as e:
            _SC_JOBS[job_id].update({"status": "error", "output": str(e)})
            _ep = (_analysis_note + "\n────────────\n") if _analysis_note else ""
            try:
                _et = f" · ⏱ {_elapsed_str()}"   # 시작 전 예외면 미정의 → 무시
            except NameError:
                _et = ""
            notify(f"{_ep}⚠️ 스크리너 오류: {e}{_et}")

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

    # 평일 매일 07:30 KST 스케줄러 (07:30~07:32 윈도우)
    # 유니버스 확대(각 1000)로 40~60분 소요 → 07:30 시작 시 08:10~08:30 종료,
    # 장 시작(09:00) 전 충분한 여유 확보.
    def _screener_scheduler():
        from stock_bot.market_calendar import is_trading_day as _is_trading_day
        while True:
            _time.sleep(30)
            try:
                now = datetime.now(tz=_KST)
                # 거래일 07:30 KST — 30~32분 윈도우로 30초 슬립 오차 흡수 (공휴일·임시휴장 제외)
                if _is_trading_day(now) and now.hour == 7 and 30 <= now.minute <= 32:
                    today_str = now.strftime("%Y-%m-%d")
                    last_str = _SC_LAST_RUN_FILE.read_text(encoding="utf-8").strip() if _SC_LAST_RUN_FILE.exists() else ""
                    if last_str != today_str:
                        _SC_LAST_RUN_FILE.write_text(today_str, encoding="utf-8")
                        _trigger_screener_auto(f"평일 자동 실행 07:30 ({now.strftime('%a')})")
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

    @app.post("/api/screener/ingest")
    async def api_screener_ingest(request: Request):
        """CI(원격 스코어링)가 screener.py 로그를 실시간 push 하는 수신구(cloudflared 경유).

        파이 LAN-only라 인바운드는 quick tunnel 로만 들어온다. 공유 시크릿 헤더
        (X-Ingest-Secret)로 검증하고, 본문 run_token 에 해당하는 실행 버퍼의 누적
        전체(content)를 갱신한 뒤 대기 중인 폴링 루프를 깨운다. content 는 항상 헤더로
        시작하는 단조 증가 문자열이라 루프가 content[_consumed:] 증분만 안전 소비한다
        (gist 와 동형). 종료는 content 에 CI가 붙인 __SCREENER_CI_DONE__ 센티넬로 감지하므로
        여기서 done 을 세팅하지 않는다(최종 결과 JSON 소비 전 조기 종료 방지). 이 엔드포인트는
        로그 버퍼 갱신 외 어떤 동작도 하지 않는다(인바운드 보안 표면 최소화)."""
        import os as _o
        _secret = (_o.environ.get("SCREENER_CI_INGEST_SECRET") or "").strip()
        if not _secret or request.headers.get("X-Ingest-Secret", "") != _secret:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            _body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        _tok = str(_body.get("run_token") or "")
        _run = _SC_REMOTE_RUNS.get(_tok)
        if not _run:
            # 이미 종료/취소된 run — CI 가 전송을 멈추도록 410
            return JSONResponse({"ok": False, "error": "unknown run"}, status_code=410)
        _content = _body.get("content")
        # 누적 전체만 수용(단조 증가) — 지연/재정렬된 짧은 본문은 무시해 접두사 불변 보장.
        if isinstance(_content, str) and len(_content) >= len(_run.get("content") or ""):
            _run["content"] = _content
        _wk = _run.get("wake")
        if _wk is not None:
            _wk.set()
        return JSONResponse({"ok": True, "ack": len(_run.get("content") or "")})

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
        """실행 중인 스크리너 job 강제 종료.

        원격(CI) 실행이면 GitHub 런을 취소 요청하고 대기 스레드를 깨운다.
        로컬 실행이면 컨테이너 내 screener.py 프로세스를 죽인다.
        """
        import subprocess as _sp_cancel
        job = _SC_JOBS.get(job_id)
        if not job:
            return JSONResponse({"ok": False, "error": "job not found"})

        # ── 원격(CI) 실행 취소 ─────────────────────────────────────────────
        _tok = next(
            (t for t, r in _SC_REMOTE_RUNS.items() if r.get("job_id") == job_id),
            None,
        )
        if _tok is not None:
            _run = _SC_REMOTE_RUNS.get(_tok) or {}
            _run["cancelled"] = True
            _ci_ok, _ci_msg = _cancel_ci(_tok)   # best effort (못 찾아도 파이는 즉시 취소)
            _ev = _run.get("done")
            if _ev is not None:
                _ev.set()                         # 대기 중인 _run_sc_job 깨우기
            _wk = _run.get("wake")
            if _wk is not None:
                _wk.set()                         # push 모드 폴링 루프 즉시 깸
            return JSONResponse({"ok": True, "remote": True,
                                 "ci_cancelled": _ci_ok, "ci_msg": _ci_msg})

        # ── 로컬 실행 취소 ─────────────────────────────────────────────────
        # 1) 취소 플래그 + 상태를 **먼저 무조건** 세팅 — _run_sc_job 스레드가 단계
        #    경계에서 이 플래그를 보고 뒷 단계로 진입하지 않는다. 상태를 여기서
        #    확정해야 다른 탭/새로고침의 /latest 가 "실행중"으로 되돌아가지 않는다.
        # 2) 실행 중인 subprocess 를 **저장해둔 핸들로 직접 kill** — 컨테이너에 pkill
        #    (procps)이 없으면 예외로 취소가 통째로 실패하던 버그를 제거. 장전분석
        #    (market_analysis.py, 최대 10분) 도중에도 즉시 멈춘다.
        job["cancelled"] = True
        _SC_JOBS[job_id].update({"status": "error", "output": "사용자 취소"})
        _p = job.get("proc")
        if _p is not None:
            try:
                if _p.poll() is None:
                    _p.kill()
            except Exception as _ke:
                logger.warning("스크리너 취소 kill 실패: {}", _ke)
        # 보조: pkill 이 있으면 자식 프로세스까지 정리(없거나 실패해도 무해).
        try:
            _sp_cancel.run(["pkill", "-9", "-f", "screener.py|market_analysis.py"],
                           timeout=5)
        except Exception:
            pass
        return JSONResponse({"ok": True, "killed": True})

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
    async def logs_stream(source: str = "bot", tail: int = 200):
        """SSE: stock_bot.log / stock_web.log / screener(메모리 버퍼) 실시간 스트리밍.

        tail: 접속 시 먼저 보내줄 최근 줄 수 (50~5000, 로그탭 표시 줄 수 설정과 연동).
        """
        tail = max(50, min(int(tail or 200), 5000))
        if source == "screener":
            # ── 스크리너: 파일 대신 in-memory 버퍼(_SC_STREAM_BUF)에서 직접 읽음 ──
            # 파일 I/O 실패와 무관하게 항상 출력 표시.
            async def generate():
                try:
                    # 최근 tail줄 전송 (cursor 기반 — 재연결 시 중복 없음).
                    # 한 이벤트로 묶어 전송 — 줄별 이벤트는 프론트가 도착 순서대로
                    # 그리며 매번 스크롤해 '쭈르륵 내려가는' 잔상이 보였다.
                    cursor = max(0, len(_SC_STREAM_BUF) - tail)
                    init = _SC_STREAM_BUF[cursor:]
                    if init:
                        yield "".join(f"data: {line}\n" for line in init) + "\n"
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

        # ── 봇/웹/대장주 로그: 파일 tail ─────────────────────────────────────────
        if source == "web":
            log_path = Path("/app/logs/stock_web.log")
        elif source == "leader":
            log_path = Path("/app/logs/stock_leader.log")
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

                # 최근 tail줄 먼저 전송 — 서브줄(VWAP/RSI/BB 등) 포함 전체 전송.
                # 한 이벤트로 묶어 전송(SSE 는 연속 data: 줄을 \n 으로 합쳐 전달)
                # — 줄별 이벤트는 프론트가 순서대로 그리며 '쭈르륵' 스크롤이 보였다.
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                init = [s for s in (line.rstrip() for line in lines[-tail:]) if s]
                if init:
                    yield "".join(f"data: {s}\n" for s in init) + "\n"

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

    @app.get("/api/symbol-search")
    def api_symbol_search(q: str = ""):
        """종목명/코드 검색 → 코스피·코스닥 후보 목록(파라미터 탭 종목 추가용).

        반환: [{"code","name","market","symbol"}]. search_stocks 가 입력 시점에만
        네이버 자동완성을 1회 호출(전체 마스터 메모리 적재 없음).
        """
        from stock_bot.names import search_stocks
        return JSONResponse(search_stocks(q))

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
        # 전략별 순손익(실현+미실현) 분리 — 대시보드 초기 렌더와 동일 키 제공.
        # 포지션은 5초 TTL 캐시 경유 — 과거엔 _live_positions() 직접 호출이라
        # 탭마다 60초 폴링이 KIS 를 중복으로 때렸다(스레드풀 고갈 기여).
        now = time.time()
        pos = _POSITIONS_CACHE["data"]
        if pos is None or now - _POSITIONS_CACHE["at"] >= _POSITIONS_CACHE_TTL:
            fresh = _live_positions()
            if fresh is not None:
                _POSITIONS_CACHE["data"] = fresh
                _POSITIONS_CACHE["at"] = now
                pos = fresh
        _apply_strategy_split(perf, pos or [])
        return JSONResponse(perf)

    _quotes_cache: dict = {"ts": 0.0, "data": []}

    @app.get("/api/quotes")
    def api_quotes():
        """종목별 현재가 조회 — 네이버 실시간 시세(표시 전용, KIS 유량과 분리).

        과거엔 KIS inquire-price 를 종목 직렬 조회해 모의 1건/초 한도(웹·봇 공유)에
        걸려 프론트 8초 타임아웃→'갱신 지연' 배지가 떴다. 네이버 배치 폴링은
        한 번의 호출(수십 ms)로 전 종목을 받고 한도가 없어 훨씬 빠릿하다.
        매매는 그대로 KIS 사용 — 여기는 화면 표시 전용이라 영향 없음.
        """
        import time
        from datetime import datetime
        from stock_bot.market_calendar import is_trading_day
        # 휴장일에는 직전 종가만 반복 조회되므로 시세를 표시하지 않음
        if not is_trading_day(datetime.now()):
            return JSONResponse({"market_closed": True, "quotes": []})
        now = time.monotonic()
        # 캐시 0.9초 — 프론트 1초 폴링이 거의 매번 새 값을 받되, 다중 접속자는
        # 합쳐서 네이버 호출을 최대 초당 1회로 묶는다(과도 호출 방지).
        if now - _quotes_cache["ts"] < 0.9 and _quotes_cache["data"]:
            return JSONResponse({"quotes": _quotes_cache["data"]})
        try:
            from stock_bot.names import get_name
            from stock_bot.broker import naver_quote
            # 스톡봇 종목 + 대장주 바스켓 (중복 제외, 전략 태그 부여)
            leader = _leader_today()
            seen = {s.split(".")[0] for s in settings.symbols}
            targets = [(s, get_name(s), "stock") for s in settings.symbols]
            for m in leader["basket"]:
                if m["code"] not in seen:
                    seen.add(m["code"])
                    targets.append((m["code"], m["name"] or get_name(m["code"]), "leader"))
            # 전 종목 한 번에 조회
            quotes = naver_quote.fetch_quotes([t[0] for t in targets])
            if not quotes:
                # 네이버 일시 실패 — 직전 캐시 유지(무중단)
                return JSONResponse({"error": "quote source unavailable",
                                     "quotes": _quotes_cache["data"]})
            results = []
            for sym, nm, strat in targets:
                q = quotes.get(sym.split(".")[0])
                results.append({
                    "symbol": sym,
                    "name": nm,
                    "price": q["price"] if q else None,
                    "change_pct": q["change_pct"] if q else None,
                    "strategy": strat,
                })
            _quotes_cache["ts"] = now
            _quotes_cache["data"] = results
        except Exception as e:
            return JSONResponse({"error": str(e), "quotes": _quotes_cache["data"]})
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
        """운영환경 실시간 스냅샷 — 핫리로드된 settings 를 그대로 반영(≤1초 폴링용)."""
        return JSONResponse({
            # 🤖 스톡봇
            "dry_run": settings.trade_dry_run,
            "env": settings.kis_env,
            "candle": settings.live_candle,
            "candle_minutes": settings.live_candle_minutes,
            "interval": settings.live_interval_minutes,
            "news_enabled": settings.news_enabled,
            # 👑 대장주봇
            "leader_enabled": bool(getattr(settings, "leader_trade_enabled", False)),
            "leader_interval": settings.leader_interval_min,
            "leader_budget": settings.leader_budget_krw,
            "leader_tp": settings.leader_tp_pct,
            "leader_stop": settings.leader_stop_buf_pct,
            "leader_close": settings.leader_close_time,
        })

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
        # 전략별 원금: 둘 중 하나라도 들어오면 반영하고 INITIAL = 합으로 자동 동기화
        if payload.stock_capital is not None or payload.leader_capital is not None:
            stock_cap = (
                payload.stock_capital
                if payload.stock_capital is not None
                else settings.stock_capital_krw
            )
            leader_cap = (
                payload.leader_capital
                if payload.leader_capital is not None
                else settings.leader_capital_krw
            )
            if stock_cap < 0 or leader_cap < 0:
                raise HTTPException(400, "capital must be >= 0")
            settings.stock_capital_krw = stock_cap  # type: ignore[assignment]
            settings.leader_capital_krw = leader_cap  # type: ignore[assignment]
            settings.initial_capital_krw = stock_cap + leader_cap  # type: ignore[assignment]
            _update_override_key("STOCK_CAPITAL_KRW", str(int(stock_cap)))
            _update_override_key("LEADER_CAPITAL_KRW", str(int(leader_cap)))
            _update_override_key("INITIAL_CAPITAL_KRW", str(int(stock_cap + leader_cap)))
            logger.info(
                "전략별 원금 변경: 스톡봇 {}원 / 대장주 {}원 (합 {}원) → .env.overrides 반영",
                int(stock_cap), int(leader_cap), int(stock_cap + leader_cap),
            )
        if payload.fee_buy_pct is not None:
            settings.trade_fee_buy_pct = payload.fee_buy_pct  # type: ignore[assignment]
            _update_override_key("TRADE_FEE_BUY_PCT", str(payload.fee_buy_pct))
        if payload.fee_sell_pct is not None:
            settings.trade_fee_sell_pct = payload.fee_sell_pct  # type: ignore[assignment]
            _update_override_key("TRADE_FEE_SELL_PCT", str(payload.fee_sell_pct))
        if not updates:
            if (payload.dry_run is None and payload.initial_capital is None
                    and payload.stock_capital is None and payload.leader_capital is None
                    and payload.fee_buy_pct is None and payload.fee_sell_pct is None):
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
