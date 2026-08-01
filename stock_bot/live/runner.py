"""실시간 거래 러너.

장중 1분마다 실행하지는 않고, 기본 15분 주기로 일봉 데이터를 당겨 시그널을 계산한다.
KRX 정규장 (09:00 ~ 15:30 KST) 에만 동작.
"""
from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from stock_bot.market_calendar import KST as _KST, utcnow as _utcnow


def _now_kst() -> str:
    return datetime.now(tz=_KST).strftime("%Y-%m-%d %H:%M KST")

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from stock_bot.broker import KISBroker
from stock_bot.broker.naver_minute import fetch_prev_ohlcv
from stock_bot.broker import naver_index
from stock_bot.config import settings
from stock_bot.indicators import atr_from_ohlcv
from stock_bot.live import chart_snapshot
from stock_bot.live import position_owner
from stock_bot.live.backup import run_backup
from stock_bot.live.review import run_daily_review
# 틱 로그·서술문 포매팅(표시 전용)은 tick_log 로 분리 — _tick 이 그대로 호출.
from stock_bot.live.tick_log import _build_narrative, _build_tick_log
from stock_bot.names import get_name
from stock_bot.news import (
    fetch_naver_news,
    init_news_db,
    news_exists,
    news_title_exists,
    recent_news_articles,
    recent_sentiment_dynamic,
    save_news,
    score_sentiment,
)
from stock_bot.news.store import get_latest_news_ts
from stock_bot.news.sentiment import score_sentiment_llm_batch
from stock_bot.notify import metrics, notify
from stock_bot.sizing import SizingResult, atr_sizing, fixed_amount, fixed_fraction
from stock_bot.costs import init_costs_db
from stock_bot.storage import init_db, record_trade
from stock_bot.strategy import MACrossSignal, decide_from_settings, EnsembleConfig
from stock_bot.strategy.ma_cross import Decision

# 추가매수 일별 카운터 (메모리, 자정 KST 기준 자동 리셋)
_add_buy_count: dict[str, int] = {}
_add_buy_date: dict[str, str] = {}

# entry_block 강제매도 하루 1회 제한 (symbol → "YYYY-MM-DD")
_force_sell_date: dict[str, str] = {}

# 종목별 EnsembleConfig 유지 (st_last_direction 등 틱 간 상태 보존)
_ensemble_cfgs: dict[str, EnsembleConfig] = {}

# 30분봉 EMA20 추세 판단 캐시: symbol → (last_30m_bar_time, is_downtrend)
# 30분 경계(XX:00, XX:30)에만 갱신, 그 사이 틱은 캐시값 재사용
_htf_trend_cache: dict[str, tuple] = {}

# 손절 후 재진입 쿨다운: symbol → datetime (마지막 손절 시각)
_last_stop_loss_at: dict[str, datetime] = {}

# 분할 익절 발동 날짜 추적 (symbol → date str, 하루 1회 제한)
_take_profit_fired: dict[str, str] = {}

# 종목별 초기 진입 stop_pct 잠금 (포지션 보유 중 stop_pct 고정용)
_locked_stop_pct: dict[str, float] = {}
# 일봉 ATR 캐시: symbol → (date, atr_value). 분봉 모드 손절/사이징용 (당일 분봉은 봉수 부족).
_daily_atr_cache: dict[str, tuple] = {}


def _daily_atr(broker: KISBroker, symbol: str, period: int) -> float:
    """일봉 ATR (당일 1회 캐시). 분봉 모드에서 9:40 등 장초반 ATR 워밍업 부족 보완용.

    당일 N분봉은 9:40 에 8봉뿐이라 ATR(14) 가 안 데워진다. 변동성 지표인 ATR 은
    당일/전일 무관하므로 일봉으로 안정적으로 추정한다.
    """
    today = datetime.now(tz=_KST).date()
    c = _daily_atr_cache.get(symbol)
    if c and c[0] == today:
        return c[1]
    val = 0.0
    try:
        daily = broker.get_daily_ohlcv(symbol, count=period + 10)
        # KIS 일봉은 newest-first → atr_from_ohlcv 는 오래된→최신 기대 → reversed
        val = atr_from_ohlcv(list(reversed(daily)), period=period)
    except Exception as exc:  # noqa: BLE001 — 실패 시 0.0 → settings 기본 손절폭 사용
        logger.debug("{}: 일봉 ATR 실패: {}", symbol, exc)
    _daily_atr_cache[symbol] = (today, val)
    return val


# 종목 일봉 게이트 캐시: symbol → (date, blocked_bool). 당일 1회 KIS 일봉 조회(유량 보호).
_daily_gate_cache: dict[str, tuple] = {}


def _stock_daily_gate_down(broker: KISBroker, symbol: str) -> bool:
    """그 종목 일봉이 하락추세인지 (당일 1회 캐시).

    차단 조건: 최근 종가 < MA  AND  MA 가 slope_days 새 slope_pct% 이상 하락.
    실패 시 False(차단 안 함). 당일 1회만 조회하므로 장중 값은 아침 스냅샷 = 전일 종가 기준
    (백테스트의 전일기준 .shift(1) 과 동치).
    """
    today = datetime.now(tz=_KST).date()
    c = _daily_gate_cache.get(symbol)
    if c and c[0] == today:
        return c[1]
    ma_n = settings.stock_daily_gate_ma
    slope_d = settings.stock_daily_gate_slope_days
    blocked = False
    try:
        daily = broker.get_daily_ohlcv(symbol, count=ma_n + slope_d + 10)
        # KIS 일봉 newest-first → 오래된→최신 종가 배열로 정렬
        closes = [float(d["close"]) for d in reversed(daily) if d.get("close")]
        if len(closes) >= ma_n + slope_d:
            s = pd.Series(closes)
            ma = s.rolling(ma_n).mean()
            ma_now = float(ma.iloc[-1])
            ma_prev = float(ma.iloc[-1 - slope_d])
            below = closes[-1] < ma_now
            steep = ma_now < ma_prev * (1 - settings.stock_daily_gate_slope_pct / 100)
            blocked = bool(below and steep)
    except Exception as exc:  # noqa: BLE001 — 실패 시 차단 안 함
        logger.debug("{}: 종목 일봉 게이트 조회 실패: {}", symbol, exc)
    _daily_gate_cache[symbol] = (today, blocked)
    return blocked

# 매도 지연: 다음 봉 시가 체결 (일반 앙상블 매도만, 손절/강제매도 제외)
# symbol → {"decision": Decision, "sell_qty": int, "avg_price": float}
_pending_sell: dict[str, dict] = {}

# 직전 성공 잔고 캐시 (symbol → (qty, avg)). KIS 잔고 조회가 타임아웃으로 실패해도
# 틱이 통째로 죽지 않도록, 같은 날 직전 성공분으로 폴백하기 위함.
# 잔고는 봇 자기 매매로만 바뀌므로 평상시엔 캐시가 정확하고, 다음 성공 조회에 자동 교정된다.
_last_positions: dict[str, tuple[int, float]] = {}
_last_positions_date: str | None = None

# 호가창 캐시: symbol → (timestamp_sec, orderbook_dict)
# 틱마다 API 한 번씩 호출하지 않도록 30초 TTL 캐시
_orderbook_cache: dict[str, tuple[float, dict]] = {}


def _mark_stop_loss(symbol: str) -> None:
    """손절 발생 시각 기록."""
    _last_stop_loss_at[symbol] = datetime.now(tz=_KST)


def _is_in_stop_loss_cooldown(symbol: str) -> tuple[bool, float]:
    """손절 후 쿨다운 중인지 확인.

    Returns: (쿨다운 중 여부, 남은 분)
    """
    cooldown_min = settings.post_stoploss_cooldown_min
    if cooldown_min <= 0:
        return False, 0.0
    last_at = _last_stop_loss_at.get(symbol)
    if last_at is None:
        return False, 0.0
    elapsed = (datetime.now(tz=_KST) - last_at).total_seconds() / 60.0
    remaining = cooldown_min - elapsed
    return remaining > 0, max(0.0, remaining)


def _has_force_sold_today(symbol: str) -> bool:
    today = datetime.now(tz=_KST).strftime("%Y-%m-%d")
    return _force_sell_date.get(symbol) == today


def _mark_force_sold(symbol: str) -> None:
    today = datetime.now(tz=_KST).strftime("%Y-%m-%d")
    _force_sell_date[symbol] = today


def _get_add_buy_count(symbol: str) -> int:
    today = datetime.now(tz=_KST).strftime("%Y-%m-%d")
    if _add_buy_date.get(symbol) != today:
        _add_buy_count[symbol] = 0
        _add_buy_date[symbol] = today
    return _add_buy_count.get(symbol, 0)


def _increment_add_buy(symbol: str) -> None:
    today = datetime.now(tz=_KST).strftime("%Y-%m-%d")
    _add_buy_date[symbol] = today
    _add_buy_count[symbol] = _add_buy_count.get(symbol, 0) + 1


# .env / .env.overrides 변경 감시용 (시작 시 실제 mtime으로 초기화해 첫 실행 오감지 방지)
_ENV_PATH = None
_root = Path(__file__).resolve().parents[2]
_ENV_MTIME = (_root / ".env").stat().st_mtime if (_root / ".env").exists() else 0.0
_ovr = _root / ".env.overrides"
_OVERRIDE_MTIME = _ovr.stat().st_mtime if _ovr.exists() else 0.0
_ENV_INITIALIZED = False  # 첫 로드는 초기화(로그 생략), 이후부터 변경으로 간주



def _parse_env_file(path) -> dict[str, str]:
    """간단한 KEY=VALUE 파서. 주석/빈줄/따옴표 제거."""
    out: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().split("#", 1)[0].strip()  # 인라인 주석 제거
            if v and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            out[k] = v
    except OSError:
        pass
    return out


# 런타임 교체 가능한 필드: (env 키, settings 속성, 변환함수)
_HOT_FIELDS = (
    ("TRADE_DRY_RUN", "trade_dry_run", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("TRADE_STRATEGY", "trade_strategy", str),
    ("POSITION_SIZING", "position_sizing", str),
    ("ENSEMBLE_MIN_BUY_VOTES", "ensemble_min_buy_votes", int),
    ("ENSEMBLE_MIN_SELL_VOTES", "ensemble_min_sell_votes", int),
    ("ENSEMBLE_BUY_THRESHOLD", "ensemble_buy_threshold", float),
    ("ENSEMBLE_SELL_THRESHOLD", "ensemble_sell_threshold", float),
    ("TRADE_STOP_LOSS_PCT", "trade_stop_loss_pct", float),
    ("ATR_STOP_LOSS_ENABLED", "atr_stop_loss_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("ATR_PERIOD", "atr_period", int),
    ("ATR_STOP_MULTIPLIER", "atr_stop_multiplier", float),
    ("ATR_STOP_MAX_PCT", "atr_stop_max_pct", float),
    ("ENSEMBLE_VOLUME_FILTER_ENABLED", "ensemble_volume_filter_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("ENSEMBLE_VOLUME_MA_PERIOD", "ensemble_volume_ma_period", int),
    ("ENSEMBLE_VOLUME_HIGH_RATIO", "ensemble_volume_high_ratio", float),
    ("ENSEMBLE_VOLUME_LOW_RATIO", "ensemble_volume_low_ratio", float),
    ("ENSEMBLE_VOLUME_SCORE_BOOST", "ensemble_volume_score_boost", float),
    ("ENSEMBLE_VOLUME_SCORE_PENALTY", "ensemble_volume_score_penalty", float),
    ("ENTRY_BLOCK_ENABLED", "entry_block_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("ENTRY_BLOCK_START", "entry_block_start", str),
    ("ENTRY_BLOCK_END", "entry_block_end", str),
    ("ENTRY_BLOCK_MIN_PROFIT_TO_SELL_PCT", "entry_block_min_profit_to_sell_pct", float),
    ("ENTRY_BLOCK_FORCE_SELL_FRACTION", "entry_block_force_sell_fraction", float),
    ("CLOSE_BLOCK_ENABLED", "close_block_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("CLOSE_BLOCK_START", "close_block_start", str),
    ("TRADE_RSI_PERIOD", "trade_rsi_period", int),
    ("TRADE_RSI_OVERSOLD", "trade_rsi_oversold", float),
    ("TRADE_RSI_OVERBOUGHT", "trade_rsi_overbought", float),
    ("TRADE_VWAP_BAND", "trade_vwap_band", float),
    ("TRADE_VWAP_SELL_BAND", "trade_vwap_sell_band", float),
    ("TRADE_VWAP_ST_BULL_SELL_BAND", "trade_vwap_st_bull_sell_band", float),
    ("TRADE_VWAP_WARMUP_BARS", "trade_vwap_warmup_bars", int),
    ("TRADE_SUPERTREND_PERIOD", "trade_supertrend_period", int),
    ("TRADE_SUPERTREND_MULT", "trade_supertrend_mult", float),
    ("TRADE_BB_WINDOW", "trade_bb_window", int),
    ("TRADE_BB_K", "trade_bb_k", float),
    ("TRADE_BB_CONSEC", "trade_bb_consec", int),
    ("TRADE_CASH_PER_TRADE", "trade_cash_per_trade", int),
    ("LIVE_INTERVAL_MINUTES", "live_interval_minutes", int),
    ("LIVE_CANDLE", "live_candle", str),
    ("SELL_ON_NEXT_OPEN", "sell_on_next_open", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("NEWS_ENABLED", "news_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("NEWS_LOOKBACK_HOURS", "news_lookback_hours", int),
    ("ENSEMBLE_NEWS_VETO_THRESHOLD", "ensemble_news_veto_threshold", float),
    ("ENSEMBLE_NEWS_STRONG_NEG_RATIO", "ensemble_news_strong_neg_ratio", float),
    ("ENSEMBLE_NEWS_WEIGHT", "ensemble_news_weight", float),
    ("NEWS_PREFER_LLM", "news_prefer_llm", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("OVERNIGHT_SELL_THRESHOLD", "overnight_sell_threshold", float),
    ("OVERNIGHT_MIN_SELL_VOTES", "overnight_min_sell_votes", int),
    ("DAILY_CONTEXT_PROFIT_GATE_PCT", "daily_context_profit_gate_pct", float),
    ("DAILY_CONTEXT_AVWAP_PCT", "daily_context_avwap_pct", float),
    ("DAILY_CONTEXT_PDH_PCT", "daily_context_pdh_pct", float),
    ("DAILY_CONTEXT_PDC_PCT", "daily_context_pdc_pct", float),
    ("HTF_BLOCK_ENABLED",         "htf_block_enabled",         lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("HTF_BLOCK_TF_MINUTES",      "htf_block_tf_minutes",      int),
    ("HTF_BLOCK_ADX_PERIOD",      "htf_block_adx_period",      int),
    ("HTF_BLOCK_ADX_THRESHOLD",   "htf_block_adx_threshold",   float),
    ("HTF_MA_OVERRIDE_ENABLED",  "htf_ma_override_enabled",  lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("HTF_MA_OVERRIDE_SPAN",     "htf_ma_override_span",     int),
    ("HTF_MA_OVERRIDE_PCT",      "htf_ma_override_pct",      float),
    ("REGIME_BLOCK_ENABLED",     "regime_block_enabled",     lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("REGIME_MA_PERIOD",         "regime_ma_period",         int),
    ("REGIME_MOM_DAYS",          "regime_mom_days",          int),
    ("ADD_BUY_ENABLED", "add_buy_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("ADD_BUY_THRESHOLD", "add_buy_threshold", float),
    ("ADD_BUY_MIN_VOTES", "add_buy_min_votes", int),
    ("ADD_BUY_MAX_COUNT", "add_buy_max_count", int),
    ("ADD_BUY_FRACTION", "add_buy_fraction", float),
    ("ADD_BUY_MAX_POSITION_PCT", "add_buy_max_position_pct", float),
    ("ADD_BUY_REQUIRE_TREND_AGREE", "add_buy_require_trend_agree", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("ADD_BUY_INHERIT_INITIAL_STOP", "add_buy_inherit_initial_stop", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("POST_STOPLOSS_COOLDOWN_MIN", "post_stoploss_cooldown_min", int),
    ("TAKE_PROFIT_ENABLED", "take_profit_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("TAKE_PROFIT_PCT", "take_profit_pct", float),
    ("TAKE_PROFIT_FRACTION", "take_profit_fraction", float),
    ("POSITION_FRACTION", "position_fraction", float),
    ("ACCOUNT_SIZE_KRW", "account_size_krw", float),
    ("DAILY_CONTEXT_TREND_BONUS", "daily_context_trend_bonus", float),
    ("ENSEMBLE_WEIGHTS", "ensemble_weights", str),
    ("ENSEMBLE_MACD_ENABLED", "ensemble_macd_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("ENSEMBLE_MACD_WEIGHT", "ensemble_macd_weight", float),
    ("ENSEMBLE_MACD_FAST", "ensemble_macd_fast", int),
    ("ENSEMBLE_MACD_SLOW", "ensemble_macd_slow", int),
    ("ENSEMBLE_MACD_SIGNAL", "ensemble_macd_signal", int),
    ("NEWS_PAGES_PER_SYMBOL", "news_pages_per_symbol", int),
    # 종목 목록 — 스크리너 자동 업데이트 시 핫리로드
    ("SYMBOLS", "trade_symbols", str),
    # 대장주 눌림목 전략 (leader_trader)
    ("LEADER_TRADE_ENABLED", "leader_trade_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("LEADER_BUDGET_KRW", "leader_budget_krw", float),
    ("LEADER_INTERVAL_MIN", "leader_interval_min", int),
    ("LEADER_W", "leader_w", int),
    ("LEADER_STOP_BUF_PCT", "leader_stop_buf_pct", float),
    ("LEADER_TP_PCT", "leader_tp_pct", float),
    ("LEADER_MAX_PULL_PCT", "leader_max_pull_pct", float),
    ("LEADER_FIB_PCT", "leader_fib_pct", float),
    ("LEADER_ANCHOR", "leader_anchor", str),
    ("LEADER_ANCHOR_EMA", "leader_anchor_ema", int),
    ("LEADER_ANCHOR_TOL", "leader_anchor_tol", float),
    ("LEADER_VOLFILTER", "leader_volfilter", float),
    ("LEADER_SWITCH_ENABLED", "leader_switch_enabled",
     lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("LEADER_SWITCH_INTERVAL_MIN", "leader_switch_interval_min", int),
    ("LEADER_SWITCH_UNTIL", "leader_switch_until", str),
    ("LEADER_SWITCH_WATCH_SECTORS", "leader_switch_watch_sectors", int),
    ("LEADER_SWITCH_HYSTERESIS", "leader_switch_hysteresis", int),
    ("LEADER_SWITCH_MOVE_MAX_PCT", "leader_switch_move_max_pct", float),
    ("LEADER_RECLAIM", "leader_reclaim", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("LEADER_TOP3_RATIO", "leader_top3_ratio", float),
    ("LEADER_BAR_RANGE_PCT", "leader_bar_range_pct", float),
    ("LEADER_CLOSE_TIME", "leader_close_time", str),
    ("LEADER_OWN_SYMBOL_PRIORITY", "leader_own_symbol_priority", lambda v: v.lower() in ("1", "true", "yes", "on")),
    # LLM 백엔드 스위치 (api | claude_code) — 파라미터탭 저장 즉시 핫리로드로 반영
    ("LLM_BACKEND", "llm_backend", str),
    # LLM 모델 선택 (haiku|sonnet|opus|fable) — 기능별, 파라미터탭 저장 즉시 핫리로드
    ("PREMARKET_REVIEW_MODEL", "premarket_review_model", str),
    ("DAILY_REVIEW_MODEL", "daily_review_model", str),
    ("NEWS_SENTIMENT_MODEL", "news_sentiment_model", str),
    # Claude API 예산 — 크레딧 충전 후 파라미터탭에서 갱신 (비용 리포트 잔여 계산용)
    ("API_BUDGET_USD", "api_budget_usd", float),
    # 충전 리셋 시점 (예산 저장 시 app.py가 자동 기록) — 잔여 = 충전액 − 리셋 이후 사용액
    ("API_BUDGET_RESET_AT", "api_budget_reset_at", float),
    # 성과 측정용 초기 자금 (웹 대시보드 수익률% 분모) — 웹 워처 핫리로드
    ("INITIAL_CAPITAL_KRW", "initial_capital_krw", float),
    ("STOCK_CAPITAL_KRW", "stock_capital_krw", float),
    ("LEADER_CAPITAL_KRW", "leader_capital_krw", float),
)

# ── 워처 범위(scope) 분리 ───────────────────────────────────────────────────
# 프로세스별로 자기 키만 핫리로드·로깅해 로그 노이즈 제거.
#   · 스톡봇 컨테이너 → scope="stock" : LEADER_* 와 표시전용 자금키 제외
#   · 대장주 컨테이너 → scope="leader": LEADER_* 키만
#   · 웹 컨테이너     → scope="all"   : 전부(대시보드 표시용)
# 동작 자체는 원래도 프로세스별 settings 가 독립이라 영향 없음 — 로그만 깔끔해짐.
_LEADER_KEYS = frozenset({
    "LEADER_TRADE_ENABLED", "LEADER_BUDGET_KRW", "LEADER_INTERVAL_MIN",
    "LEADER_W", "LEADER_STOP_BUF_PCT", "LEADER_TP_PCT",
    "LEADER_MAX_PULL_PCT", "LEADER_FIB_PCT",
    "LEADER_ANCHOR", "LEADER_ANCHOR_EMA", "LEADER_ANCHOR_TOL", "LEADER_VOLFILTER",
    "LEADER_RECLAIM", "LEADER_TOP3_RATIO",
    "LEADER_BAR_RANGE_PCT", "LEADER_CLOSE_TIME",
    "LEADER_SWITCH_ENABLED", "LEADER_SWITCH_INTERVAL_MIN", "LEADER_SWITCH_UNTIL",
    "LEADER_SWITCH_WATCH_SECTORS", "LEADER_SWITCH_HYSTERESIS", "LEADER_SWITCH_MOVE_MAX_PCT",
    # own-symbol 우선권 토글 — 대장주봇 매매 판정(제외 vs 점유락)을 직접 좌우.
    "LEADER_OWN_SYMBOL_PRIORITY",
})
# 스톡봇·대장주봇이 함께 반영해야 하는 공용 키(_LEADER_KEYS 처럼 stock 스코프에서
# 배제하면 안 됨). SYMBOLS 는 스톡봇 매매 대상이면서, 대장주봇에도 필요하다:
# 우선권 OFF 면 겹치는 종목 제외(leader_trader own 집합), ON 이어도 점유 원장
# 정합에 쓴다. 스크리너 로테이션마다 최신값을 따라가야 재시작 없이 정합 유지.
_SHARED_KEYS = frozenset({"SYMBOLS"})
# 웹 대시보드 표시 전용(분모) — 봇 매매 로직은 읽지 않음.
_DISPLAY_ONLY_KEYS = frozenset({
    "INITIAL_CAPITAL_KRW", "STOCK_CAPITAL_KRW", "LEADER_CAPITAL_KRW",
})
_SCOPE_LABEL = {"stock": "스톡봇", "leader": "대장주봇", "all": "웹"}


def _key_in_scope(key: str, scope: str) -> bool:
    """이 워처 scope 가 해당 env 키를 반영해야 하는지."""
    if scope == "all":
        return True
    if scope == "leader":
        return key in _LEADER_KEYS or key in _SHARED_KEYS
    # scope == "stock": 대장주 전용·표시전용 키 제외, 나머지(스톡봇 사용 키)만.
    # 공용 키(_SHARED_KEYS)는 stock 도 반영 — _LEADER_KEYS 배제에 걸리지 않음.
    return key not in _LEADER_KEYS and key not in _DISPLAY_ONLY_KEYS


def _reload_env_if_changed(scope: str = "all") -> None:
    """`.env` / `.env.overrides` 변경 감지 → 핫리로드.

    도커에서 env vars 가 os.environ 에 고정되므로 pydantic Settings 재인스턴스화로는
    갱신되지 않는다. 파일을 직접 파싱해 `settings` 객체 속성을 덮어쓴다.
    우선순위: .env.overrides > .env

    scope: "stock"(스톡봇)·"leader"(대장주봇)·"all"(웹). 자기 키만 반영·로깅.
    """
    global _ENV_PATH, _ENV_MTIME, _OVERRIDE_MTIME, _ENV_INITIALIZED
    was_initialized = _ENV_INITIALIZED
    _ENV_INITIALIZED = True
    if _ENV_PATH is None:
        _ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
    if not _ENV_PATH.exists():
        return

    override_path = _ENV_PATH.parent / ".env.overrides"
    try:
        env_mtime = _ENV_PATH.stat().st_mtime
    except OSError:
        return
    try:
        ovr_mtime = override_path.stat().st_mtime if override_path.exists() else 0.0
    except OSError:
        ovr_mtime = 0.0

    if env_mtime <= _ENV_MTIME and ovr_mtime <= _OVERRIDE_MTIME:
        return
    _ENV_MTIME = env_mtime
    _OVERRIDE_MTIME = ovr_mtime

    parsed = _parse_env_file(_ENV_PATH)
    # .env.overrides 가 있으면 덮어쓰기 (더 높은 우선순위)
    if override_path.exists():
        parsed.update(_parse_env_file(override_path))

    changed: list[str] = []
    for key, attr, cast in _HOT_FIELDS:
        if key not in parsed or not _key_in_scope(key, scope):
            continue
        try:
            new_val = cast(parsed[key])
        except Exception:
            continue
        old_val = getattr(settings, attr, None)
        if old_val != new_val:
            setattr(settings, attr, new_val)
            changed.append(f"{attr}: {old_val} → {new_val}")
    if changed and was_initialized:
        logger.info(".env 변경 감지, 핫리로드[{}]: {}",
                    _SCOPE_LABEL.get(scope, scope), "; ".join(changed))



def _send_cost_report() -> None:
    """어제 KST 기준 API 비용 리포트를 Discord로 전송.

    claude_code 백엔드(구독 호출)에선 API 비용이 0이라 리포트가 무의미 —
    매일 $0 리포트가 날아오는 걸 막기 위해 건너뛴다. api 로 롤백하면 자동 재개.
    """
    from datetime import date, timedelta
    from stock_bot import llm_cli
    from stock_bot.costs import format_daily_report
    if llm_cli.use_cli():
        logger.debug("claude_code 백엔드 — API 비용 리포트 생략")
        return
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        msg = format_daily_report(yesterday)
        notify(msg)
    except Exception as exc:
        logger.warning("비용 리포트 전송 실패: {}", exc)




# 휴장일 조회용 브로커 참조 (run_live 에서 주입). KIS 달력이 주문 서버와 동일.
_holiday_broker: "KISBroker | None" = None

# exchange_calendars 가 누락하는 임시공휴일(선거일 등) 수동 보강.
# 단일 출처(stock_bot.market_calendar)에서 매 호출 시 읽어와 웹에서 추가한
# 휴장일이 재시작 없이 반영되게 한다. 모의투자 도메인은 KIS 휴장일 API
# 미지원이라 이 폴백이 실제로 동작한다.
from stock_bot.market_calendar import get_extra_holidays

# 거래일 판정 로그를 날짜당 1회만 찍기 위한 기록
_trading_day_logged: set[str] = set()


def _is_trading_day(date_kst: datetime) -> bool:
    """KRX 거래일 여부 (주말 + 공휴일 + 임시공휴일 모두 체크).

    우선순위로 판정하고, 날짜당 1회 '결과 + 출처'를 로그로 남긴다.
    1순위: KIS 국내휴장일조회 API (주문 서버와 동일한 달력, 임시공휴일까지 정확)
    2순위: 수동 보강(_EXTRA_HOLIDAYS) — exchange_calendars 가 모르는 임시공휴일
    3순위: exchange_calendars (정규 공휴일은 정확)
    4순위: 주말 여부만
    """
    if date_kst.weekday() >= 5:
        return False

    ds = date_kst.strftime("%Y-%m-%d")
    result: bool | None = None
    source = ""

    if _holiday_broker is not None:
        try:
            result = _holiday_broker.is_open_day(date_kst.strftime("%Y%m%d"))
            source = "KIS"
        except Exception:
            result = None

    if result is None:
        if ds in get_extra_holidays():
            result, source = False, "수동보강"
        else:
            try:
                import exchange_calendars as xcals
                cal = xcals.get_calendar("XKRX")
                result, source = bool(cal.is_session(ds)), "exchange_calendars"
            except Exception:
                result, source = date_kst.weekday() < 5, "주말판정(폴백)"

    if ds not in _trading_day_logged:
        _trading_day_logged.add(ds)
        logger.info(
            "거래일 판정 {} → {} (출처: {})",
            ds, "거래일" if result else "휴장", source,
        )
    return result


def _is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=_KST)
    if not _is_trading_day(now):
        return False
    # 상한은 15:30 '분 전체'를 포함 — cron 이 15:30:00 에 발화해도 스케줄러
    # 지연으로 now 가 15:30:00.x 가 되면 <=15:30:00 비교가 거짓이 되어 15:30
    # 틱이 통째로 스킵(마지막 틱이 15:25 가 됨)되던 문제 방지. 종가·동시호가 포함.
    return dtime(9, 0) <= now.time() <= dtime(15, 30, 59)


def _positions_by_symbol(broker: KISBroker) -> dict[str, tuple[int, float]]:
    # KIS API는 6자리 코드(pdno: "005930")를 반환하지만 settings.symbols는 "005930.KS" 형식.
    # 6자리 코드 → 풀 심볼 역매핑 테이블을 미리 구성해 키를 맞춤.
    code_to_sym = {s.split(".")[0]: s for s in settings.symbols}
    out: dict[str, tuple[int, float]] = {}
    for row in broker.get_positions():
        code = row.get("pdno")
        qty = int(row.get("hldg_qty", 0) or 0)
        avg = float(row.get("pchs_avg_pric", 0) or 0)
        if code and qty > 0:
            sym = code_to_sym.get(code, code)  # 알려진 종목이면 풀 심볼, 아니면 원본 코드
            out[sym] = (qty, avg)
    return out


def _get_last_buy_date(symbol: str) -> str | None:
    """TradeLog 에서 해당 종목의 마지막 매수 날짜(KST, "YYYY-MM-DD") 반환."""
    from sqlalchemy import or_, select
    from sqlalchemy.orm import Session
    from stock_bot.storage import ENGINE, TradeLog
    code = symbol.split(".")[0]  # "005930.KS" → "005930" (구버전 DB는 suffix 없이 저장)
    try:
        with Session(ENGINE) as s:
            row = s.scalars(
                select(TradeLog)
                .where(or_(TradeLog.symbol == symbol, TradeLog.symbol == code))
                .where(TradeLog.side == "buy")
                .order_by(TradeLog.ts.desc())
            ).first()
        if row is None:
            return None
        # ts는 UTC로 저장됨 → KST 변환
        kst_ts = row.ts.replace(tzinfo=timezone.utc).astimezone(_KST)
        return kst_ts.strftime("%Y-%m-%d")
    except Exception as exc:
        logger.debug("_get_last_buy_date {} failed: {}", symbol, exc)
        return None


def _get_account_value(broker: KISBroker) -> float:
    """계좌 총평가금액. 설정값이 있으면 그걸 우선, 없으면 브로커에 조회."""
    if settings.account_size_krw > 0:
        return settings.account_size_krw
    if settings.trade_dry_run:
        # dry-run 이면 브로커 조회도 실패할 수 있으니 합리적 기본값
        return max(settings.trade_cash_per_trade * 20, 10_000_000)
    return broker.get_account_total() or 10_000_000.0


def _compute_sizing(
    price: float, ohlcv: list[dict], account_value: float, atr_override: float | None = None
) -> SizingResult:
    mode = settings.position_sizing
    if mode == "fraction":
        return fixed_fraction(account_value, settings.position_fraction, price)
    if mode == "atr":
        # 분봉 모드는 일봉 ATR(atr_override) 사용 — 당일 N분봉은 장초반 봉수 부족
        atr_value = (
            atr_override if atr_override is not None
            else atr_from_ohlcv(list(reversed(ohlcv)), period=settings.atr_period)
        )
        return atr_sizing(
            account_value=account_value,
            risk_pct=settings.risk_per_trade_pct,
            atr_value=atr_value,
            stop_multiplier=settings.atr_stop_multiplier,
            price=price,
            max_position_pct=settings.max_position_pct,
        )
    return fixed_amount(settings.trade_cash_per_trade, price)


def _news_tick(broker: KISBroker | None = None) -> None:
    """각 종목의 신규 뉴스를 가져와 감성 점수와 함께 저장.

    장중(09:00~15:00 KST) 에는 5분마다 실행되며,
    critical 기사가 포착되면 해당 종목에 대해 즉시 거래 tick 을 발화한다.
    """
    _reload_env_if_changed("stock")
    if not settings.news_enabled:
        return
    trigger_symbols: set[str] = set()
    for symbol in settings.symbols:
        try:
            code = symbol.split(".")[0]  # 005930.KS → 005930 (DB 저장 형식)
            # DB 최신 기사 시각 기준 10분 여유를 두고 early stop
            last_ts = get_latest_news_ts(code)
            since = (last_ts - timedelta(minutes=10)) if last_ts else None
            items = fetch_naver_news(
                symbol, pages=settings.news_pages_per_symbol, since=since,
                relevance_filter=settings.news_relevance_filter,
            )

            # 1단계: URL·제목 중복 제거 (LLM 호출 전) — code(6자리)로 조회
            new_items = [
                item for item in items
                if not news_exists(item.symbol, item.url)
                and not news_title_exists(code, item.title)
            ]

            if not new_items:
                logger.debug("news {} new=0/{} (모두 중복)", symbol, len(items))
                continue

            # 2단계: 배치 LLM 1회 호출 (prefer_llm 일 때만)
            texts = [f"{item.title} {item.summary}" for item in new_items]
            use_llm = settings.news_prefer_llm
            if use_llm:
                batch_results = score_sentiment_llm_batch(texts, symbol=symbol)
            else:
                batch_results = [None] * len(new_items)

            new_count = 0
            crit_count = 0
            for item, llm_res, text in zip(new_items, batch_results, texts):
                # LLM 결과 있으면 사용, 없으면 키워드 폴백
                if llm_res is not None:
                    result = llm_res
                else:
                    result = score_sentiment(text, prefer_llm=False, symbol=symbol)

                if save_news(
                    item,
                    result.score,
                    result.method,
                    weight=result.weight,
                    is_critical=result.is_critical,
                ):
                    new_count += 1
                    if result.is_critical:
                        crit_count += 1
                        trigger_symbols.add(symbol)
                        logger.warning(
                            "news CRITICAL {} score={:+.2f} phrases={} title={!r}",
                            symbol,
                            result.score,
                            result.critical_phrases,
                            item.title[:60],
                        )

            logger.info(
                "news {} new={}/{} critical={} llm_batch={}",
                symbol, new_count, len(items), crit_count,
                1 if use_llm else 0,
            )
        except Exception as exc:
            logger.warning("news crawl failed for {}: {}", symbol, exc)

    # critical 기사가 있고 장중이면 해당 종목에 대해 즉시 거래 평가
    if broker and trigger_symbols and _is_market_open():
        logger.warning("critical news trigger → immediate tick for {}", trigger_symbols)
        _tick(broker, only_symbols=trigger_symbols)


def _tick(broker: KISBroker, only_symbols: set[str] | None = None) -> None:
    _reload_env_if_changed("stock")
    if not _is_market_open():
        logger.debug("market closed, skip")
        return

    # 잔고 조회는 틱의 필수 1단계(매수/매도·손절·익절 판단 모두 보유수량·평단 필요).
    # KIS(특히 모의서버) 간헐 타임아웃에 틱 전체가 죽지 않도록: 성공분은 캐시하고,
    # 실패 시 같은 날 직전 캐시로 폴백. 캐시가 없으면(장 첫 틱 등) 안전하게 스킵.
    global _last_positions, _last_positions_date
    _today_str = datetime.now(tz=_KST).strftime("%Y-%m-%d")
    try:
        positions = _positions_by_symbol(broker)
        _last_positions = dict(positions)
        _last_positions_date = _today_str
    except Exception as exc:  # noqa: BLE001 — 잔고 조회 실패가 틱을 죽이지 않게 흡수
        if _last_positions_date == _today_str and _last_positions:
            logger.warning("잔고 조회 실패({}) — 직전 잔고 캐시로 진행", exc)
            positions = dict(_last_positions)
        else:
            logger.warning("잔고 조회 실패({}) — 캐시 없음, 이번 틱 스킵", exc)
            return

    # 대장주 own-symbol 우선권: 스톡봇 점유 원장을 실제 잔고와 대조해 고아 청소.
    # (보유→미보유 = 청산 완료 → 점유 해제 → 대장주가 다시 그 종목 진입 가능)
    if settings.leader_own_symbol_priority:
        position_owner.reconcile(
            "stock", [s for s, (q, _a) in positions.items() if q > 0]
        )

    lookback = max(
        settings.trade_long_ma,
        settings.trade_ema_slow,
        settings.trade_rsi_period,
        settings.trade_macd_slow + settings.trade_macd_signal,
        settings.trade_bb_window,
        settings.trade_momentum_period,
    ) + 10
    # 분봉 모드: HTF MA 오버라이드(EMA120)용 히스토리 확보 → 최소 135봉
    if settings.live_candle == "minute":
        lookback = max(lookback, 135)

    symbols_to_run = [s for s in settings.symbols if not only_symbols or s in only_symbols]
    for symbol in symbols_to_run:
        try:
            # ── 대장주 점유 종목: 매수·매도·판단 전부 정지 (포지션 있어도 손 안 댐) ──
            # 대장주봇이 잡은 종목은 그 봇이 익절/손절까지 전담. 스톡봇은 일절 관여 안 함.
            if (settings.leader_own_symbol_priority
                    and position_owner.owner_of(symbol) == "leader"):
                logger.debug("{} [대장주 점유] 스톡봇 판단 보류", symbol)
                continue
            # ── 지연매도 체결: 이전 틱에서 큐된 매도를 이번 봉 시가(현재가)로 체결 ──
            if symbol in _pending_sell:
                _ps = _pending_sell.pop(symbol)
                _ps_qty = _ps.get("sell_qty", 0)
                _ps_avg = _ps.get("avg_price", 0.0)
                _ps_dec = _ps.get("decision")
                if _ps_qty > 0:
                    try:
                        _ps_price = float(broker.get_quote(symbol).price)
                    except Exception:
                        _ps_price = 0.0
                    if _ps_price > 0:
                        resp = broker.place_order(symbol, "sell", _ps_qty)
                        _pnl_pct = ((_ps_price - _ps_avg) / _ps_avg * 100) if _ps_avg > 0 else 0.0
                        _pnl_str = f"{'▲' if _pnl_pct >= 0 else '▼'} {_pnl_pct:+.2f}%"
                        _nm = get_name(symbol)
                        _ps_ctx = dict(_ps.get("trade_context") or {})
                        _ps_ctx["exec_price"] = _ps_price
                        _ps_ctx["signal_price"] = _ps.get("signal_price", 0)
                        _ps_ctx["signal_ts"] = _ps.get("signal_ts", "")
                        _ps_reason = (
                            f"지연매도 체결 (이전 봉 신호 → 이번 봉 시가): "
                            f"{_ps_dec.reason if _ps_dec else ''}"
                        )
                        record_trade(
                            symbol, "sell", _ps_qty, _ps_price,
                            _ps_reason,
                            json.dumps(resp, ensure_ascii=False),
                            strategy=settings.trade_strategy,
                            details=_ps_ctx,
                        )
                        metrics.orders_total.labels(symbol=symbol, side="sell", mode="dry_run" if settings.trade_dry_run else settings.kis_env).inc()
                        notify(
                            f"🔵 **매도(지연)** {symbol}{f' ({_nm})' if _nm else ''} {_ps_qty}주 @ {_ps_price:,.0f}원\n"
                            f"수익률: {_pnl_str} (평단 {_ps_avg:,.0f}원)\n"
                            f"시간: {_now_kst()}\n\n지연매도: 이전 봉 신호 → 이번 봉 시가 체결"
                        )
                        logger.info(
                            "{} 지연매도 체결: {}주 @ {:,.0f}원 (수익 {:+.2f}%)",
                            symbol, _ps_qty, _ps_price, _pnl_pct,
                        )

            ohlcv_raw: list = []  # ATR 보조용 (분봉 모드에선 일봉 ATR 별도 사용)
            _closes_src: list = []  # ohlcv_df_hist 오늘 부분 = 오늘 실 OHLC
            _prev_bars: list[dict] = []  # 어제 유사 OHLC (네이버 1분종가 합성, 오름차순)
            if settings.live_candle == "minute":
                _interval = settings.live_candle_minutes
                # ── 오늘: KIS 1분봉 페이지네이션 → N분봉 실 OHLC (newest-first, 오늘만) ──
                # VWAP·ATR(손절)·거래량은 '오늘 실봉'만 사용.
                ohlcv = broker.get_minute_ohlcv_today(symbol, interval_min=_interval)
                if not ohlcv:
                    # 페이지네이션 전부 실패 → 기존 단발 호출 폴백 (틱 스킵 방지)
                    ohlcv = broker.get_minute_ohlcv(symbol, interval_min=_interval, count=lookback)
                _closes_src = ohlcv      # ohlcv_df_hist 빌드용 (오늘 실 OHLC)
                ohlcv_raw = ohlcv
                # 차트 탭용 스냅샷(표시 전용·KIS 추가호출 없음). 실패해도 틱 불변.
                chart_snapshot.write_snapshot(symbol, _interval, ohlcv, source="live")
                # ── 어제봉 워밍업: 네이버 1분종가 → N분 유사 OHLC (부족분만) ──────
                # closes(BB/RSI/MACD/EMA120)와 ohlcv_df_hist(ST/PSAR/HTF-ADX) 겸용.
                # 유사봉이라 고저 폭이 실봉보다 약간 좁지만 ST 방향 일치율 95.6~100%
                # (당일봉만 쓰면 첫봉 상승가정 탓에 하락일 일치율 22~50%로 붕괴 — 2026-07-15 검증).
                # 스크리너가 종목을 매일 바꿔도 상태 파일 없이 어떤 종목이든 즉시 확보.
                # 실패 시 [] → 기존(오늘 봉만) 동작으로 폴백, 라이브 무중단.
                _today_closes_asc = [r["close"] for r in reversed(ohlcv)]
                _need_prev = max(0, lookback - len(_today_closes_asc))
                if _need_prev > 0:
                    try:
                        _prev_bars = fetch_prev_ohlcv(symbol, _interval, _need_prev)
                    except Exception as _npc:  # noqa: BLE001 — 실패해도 오늘 봉만으로 진행
                        logger.debug("{}: 네이버 어제봉 워밍업 실패: {}", symbol, _npc)
                closes = pd.Series([b["close"] for b in _prev_bars] + _today_closes_asc)
            else:
                ohlcv = broker.get_daily_ohlcv(symbol, count=lookback)
                _closes_src = ohlcv
                ohlcv_raw = ohlcv
                # KIS 는 최신이 앞이므로 역순 정렬 (오래된→최신)
                closes = pd.Series([row["close"] for row in reversed(_closes_src)])
            # KIS 는 최신이 앞이므로 역순 정렬 (오래된→최신)
            ohlcv_asc = list(reversed(ohlcv))
            # 분봉 모드 ATR: 당일 분봉이 충분(≥atr_period+1)하면 '분봉 ATR'(동적), 부족한 장초반엔 일봉 ATR 폴백.
            # 2026-07-03: 일봉 ATR×큰배수(12)는 항상 5%캡에 붙어 '동적'이 사실상 죽음. 분봉 ATR+낮은배수(예: 5)면
            #   손절이 실제로 변동성에 반응(≈2~3%). 대형주 백테스트서 손실↓·수익↑ 확인, 페이퍼로 실유니버스 관찰 중.
            #   당일 분봉만 사용(오버나이트 갭 제외 = 순수 장중 변동성). 보유 중엔 _locked_stop_pct 로 잠겨 안 흔들림.
            if settings.live_candle == "minute":
                if len(ohlcv_asc) >= settings.atr_period + 1:
                    _atr_value = atr_from_ohlcv(ohlcv_asc, period=settings.atr_period)
                    if _atr_value <= 0:  # 방어: 분봉 ATR 계산 실패 시 일봉으로 폴백
                        _atr_value = _daily_atr(broker, symbol, settings.atr_period)
                else:
                    _atr_value = _daily_atr(broker, symbol, settings.atr_period)
            else:
                _atr_value = atr_from_ohlcv(
                    list(reversed(ohlcv_raw if ohlcv_raw else ohlcv)), period=settings.atr_period
                )

            # ── 봉 부족이라도 장초반 강제매도는 먼저 처리 ──────────────────
            # 9:00 틱은 봉 1개뿐 → len(closes)<3 스킵 전에 entry_block 확인
            if len(closes) < 3 and settings.entry_block_enabled and ohlcv:
                _eb_now = datetime.now(tz=_KST).time()
                try:
                    _bs = dtime.fromisoformat(settings.entry_block_start)
                    _be = dtime.fromisoformat(settings.entry_block_end)
                    _qty_eb, _avg_eb = positions.get(symbol, (0, 0.0))
                    if (_bs <= _eb_now < _be and _qty_eb > 0 and _avg_eb > 0
                            and not _has_force_sold_today(symbol)):
                        if ohlcv:
                            _price_eb = float(ohlcv[0].get("close", 0) or 0)
                        else:
                            try:
                                _price_eb = broker.get_quote(symbol).price
                            except Exception:
                                _price_eb = 0.0
                        _min_p = settings.entry_block_min_profit_to_sell_pct
                        _profit_eb = (_price_eb - _avg_eb) / _avg_eb * 100 if _price_eb > 0 else 0.0
                        if _profit_eb >= _min_p:
                            _frac = settings.entry_block_force_sell_fraction
                            _sell_qty = max(1, int(_qty_eb * _frac))
                            logger.info(
                                "{} [entry-block 봉부족] 강제매도 (수익 {:+.2f}% ≥ {:.1f}%, {}주)",
                                symbol, _profit_eb, _min_p, _sell_qty,
                            )
                            resp = broker.place_order(symbol, "sell", _sell_qty)
                            _mark_force_sold(symbol)
                            record_trade(
                                symbol, "sell", _sell_qty, _price_eb,
                                f"entry-block 강제매도 {_frac:.0%} (수익 {_profit_eb:+.2f}% ≥ {_min_p:.1f}%, 봉부족)",
                                json.dumps(resp, ensure_ascii=False),
                                strategy=settings.trade_strategy,
                            )
                except Exception as _exc:
                    logger.debug("{}: entry-block 봉부족 처리 실패: {}", symbol, _exc)

            # ── 봉 부족이라도 stop-loss는 먼저 처리 ────────────────────────
            # last_price/avg_price만 있으면 손절 계산 가능 → skip 전에 체크
            if len(closes) < 3 and ohlcv:
                try:
                    _qty_sl, _avg_sl = positions.get(symbol, (0, 0.0))
                    if _qty_sl > 0 and _avg_sl > 0:
                        _price_sl = float(ohlcv[0].get("close", 0) or 0)
                        # ATR 손절 계산 (가능하면 동적, 아니면 settings 기본값)
                        _stop_pct_sl = settings.trade_stop_loss_pct
                        if settings.position_sizing == "atr" or settings.atr_stop_loss_enabled:
                            _atr_val_sl = _atr_value  # 분봉=분봉ATR(장초반 폴백 일봉), 일봉=당일ohlcv ATR
                            if _atr_val_sl > 0 and _price_sl > 0:
                                _dyn_sl = (_atr_val_sl * settings.atr_stop_multiplier) / _price_sl * 100
                                _stop_pct_sl = min(_dyn_sl, settings.atr_stop_max_pct)
                        _loss_sl = (_price_sl - _avg_sl) / _avg_sl * 100 if _price_sl > 0 else 0.0
                        if _loss_sl <= -abs(_stop_pct_sl):
                            logger.info(
                                "{} [봉부족 stop-loss] 손절 매도 (손실 {:+.2f}% ≤ -{:.2f}%, {}주)",
                                symbol, _loss_sl, _stop_pct_sl, _qty_sl,
                            )
                            resp = broker.place_order(symbol, "sell", _qty_sl)
                            record_trade(
                                symbol, "sell", _qty_sl, _price_sl,
                                f"stop-loss 손절 (손실 {_loss_sl:+.2f}% ≤ -{_stop_pct_sl:.2f}%, 봉부족)",
                                json.dumps(resp, ensure_ascii=False),
                                strategy=settings.trade_strategy,
                            )
                except Exception as _exc_sl:
                    logger.debug("{}: 봉부족 stop-loss 처리 실패: {}", symbol, _exc_sl)

            if len(closes) < 3:
                logger.warning("{}: 캔들 데이터 부족 ({}개), skip", symbol, len(closes))
                continue
            # VWAP/Supertrend 용 OHLCV DataFrame (분봉 모드에서만 의미 있음)
            ohlcv_df: pd.DataFrame | None = None
            # ST/PSAR/HTF-ADX용 히스토리 = 어제 유사봉 + 오늘 실봉
            # (백테스트 backtest_current.py의 df_slice와 동형 — 여러 날 연속봉)
            ohlcv_df_hist: pd.DataFrame | None = None
            if settings.live_candle == "minute":
                try:
                    ohlcv_df = pd.DataFrame(ohlcv_asc)[["open", "high", "low", "close", "volume"]]
                    ohlcv_df = ohlcv_df.apply(pd.to_numeric, errors="coerce")
                except Exception:
                    ohlcv_df = None
                try:
                    _hist_asc = _prev_bars + list(reversed(_closes_src))
                    ohlcv_df_hist = pd.DataFrame(_hist_asc)[["open", "high", "low", "close", "volume"]]
                    ohlcv_df_hist = ohlcv_df_hist.apply(pd.to_numeric, errors="coerce")
                except Exception:
                    ohlcv_df_hist = None
            qty, avg = positions.get(symbol, (0, 0.0))

            # DailyContext 용: 포지션 있을 때만 매수 날짜 + 전일 고가/종가 조회
            entry_date: str | None = None
            prev_day_high: float = 0.0
            prev_day_close: float = 0.0
            if qty > 0 and settings.trade_strategy == "ensemble":
                entry_date = _get_last_buy_date(symbol)
                try:
                    daily_data = broker.get_daily_ohlcv(symbol, count=3)
                    # KIS는 최신이 앞 → [0]=오늘, [1]=전일
                    if len(daily_data) >= 2:
                        prev_day_high = float(daily_data[1].get("high", 0) or 0)
                        prev_day_close = float(daily_data[1].get("close", 0) or 0)
                except Exception as exc:
                    logger.debug("{}: daily ohlcv for daily_context 실패: {}", symbol, exc)

            news_score, news_count, news_critical, news_strong_neg = (0.0, 0, 0, 0)
            if settings.news_enabled:
                news_score, news_count, news_critical, news_strong_neg = recent_sentiment_dynamic(
                    symbol, strong_neg_threshold=settings.ensemble_news_veto_threshold
                )

            # ATR 손절: position_sizing=atr 또는 atr_stop_loss_enabled=true 면 동적 계산
            # 분봉 모드는 분봉 ATR(_atr_value, 장초반엔 일봉 폴백) 사용 — 위 계산부 참고
            effective_stop_pct = settings.trade_stop_loss_pct
            _atr_val_meta: float | None = None
            _last_price_meta: float | None = None
            if settings.position_sizing == "atr" or settings.atr_stop_loss_enabled:
                atr_val = _atr_value
                last_price_tmp = float(closes.iloc[-1])
                if atr_val > 0 and last_price_tmp > 0:
                    dynamic_pct = (atr_val * settings.atr_stop_multiplier) / last_price_tmp * 100
                    effective_stop_pct = min(dynamic_pct, settings.atr_stop_max_pct)
                    _atr_val_meta = atr_val
                    _last_price_meta = last_price_tmp

            # 추가매수로 stop_pct 확대 방지: 포지션 보유 중이면 초기값 잠금 유지
            if settings.add_buy_inherit_initial_stop:
                if qty > 0:
                    _locked = _locked_stop_pct.get(symbol)
                    if _locked is not None:
                        effective_stop_pct = _locked  # 잠긴 값 사용
                    else:
                        _locked_stop_pct[symbol] = effective_stop_pct  # 첫 잠금
                else:
                    # 포지션 없으면 잠금 해제
                    _locked_stop_pct.pop(symbol, None)
            # ── HTF 하락추세 매수 차단 (ADX 기반) ───────────────────────────────
            # ADX > htf_block_adx_threshold AND -DI > +DI 일 때 신규 매수 차단
            # 매 HTF 봉 경계에만 재계산, 그 사이 틱은 캐시 재사용
            _htf_is_down = False
            if settings.htf_block_enabled and settings.live_candle == "minute" and ohlcv_df_hist is not None:
                try:
                    _tf_min    = settings.htf_block_tf_minutes
                    _adx_per   = settings.htf_block_adx_period
                    _adx_thr   = settings.htf_block_adx_threshold
                    _now_dt    = datetime.now(tz=_KST)
                    _bar_key   = _now_dt.replace(
                        minute=(_now_dt.minute // _tf_min) * _tf_min,
                        second=0, microsecond=0,
                    )
                    _cached    = _htf_trend_cache.get(symbol)
                    if _cached is None or _cached[0] != _bar_key:
                        _htf_df   = ohlcv_df_hist.copy()
                        _interval = settings.live_candle_minutes
                        _end_ts   = _now_dt.replace(second=0, microsecond=0)
                        _htf_df.index = pd.date_range(
                            end=_end_ts, periods=len(_htf_df),
                            freq=f"{_interval}min", tz=_KST,
                        )
                        _htf_r = _htf_df.resample(
                            f"{_tf_min}min", label="left", closed="left"
                        ).agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
                        ).dropna(subset=["close"])
                        _min_bars = _adx_per * 2 + 2
                        if len(_htf_r) >= _min_bars:
                            _htf_c = _htf_r.iloc[:-1]  # 현재 미완성 봉 제외
                            _hi  = _htf_c["high"]
                            _lo  = _htf_c["low"]
                            _cl  = _htf_c["close"]
                            _tr  = pd.concat([
                                _hi - _lo,
                                (_hi - _cl.shift(1)).abs(),
                                (_lo - _cl.shift(1)).abs(),
                            ], axis=1).max(axis=1)
                            _dm_p = (_hi - _hi.shift(1)).clip(lower=0)
                            _dm_m = (_lo.shift(1) - _lo).clip(lower=0)
                            _dm_p = _dm_p.where(_dm_p > _dm_m, 0)
                            _dm_m = _dm_m.where(_dm_m > _dm_p.where(_dm_p > _dm_m, 0), 0)
                            def _wilder(s, n):
                                r = s.copy().astype(float)
                                r.iloc[:n] = float("nan")
                                r.iloc[n]  = s.iloc[1:n+1].sum()
                                for i in range(n+1, len(s)):
                                    r.iloc[i] = r.iloc[i-1] - r.iloc[i-1] / n + s.iloc[i]
                                return r
                            _atr_s = _wilder(_tr, _adx_per)
                            _dip_s = _wilder(_dm_p, _adx_per)
                            _dim_s = _wilder(_dm_m, _adx_per)
                            _di_p  = (_dip_s / _atr_s * 100).replace([float("inf"), float("-inf")], float("nan"))
                            _di_m  = (_dim_s / _atr_s * 100).replace([float("inf"), float("-inf")], float("nan"))
                            _dx    = ((_di_p - _di_m).abs() / (_di_p + _di_m) * 100).replace([float("inf"), float("-inf")], float("nan"))
                            _adx_s = _wilder(_dx, _adx_per)
                            _last_adx = float(_adx_s.iloc[-1]) if not pd.isna(_adx_s.iloc[-1]) else 0.0
                            _last_dip = float(_di_p.iloc[-1])  if not pd.isna(_di_p.iloc[-1])  else 0.0
                            _last_dim = float(_di_m.iloc[-1])  if not pd.isna(_di_m.iloc[-1])  else 0.0
                            _htf_is_down = (_last_adx > _adx_thr) and (_last_dim > _last_dip)
                            _htf_trend_cache[symbol] = (_bar_key, _htf_is_down)
                            logger.debug(
                                "{} [HTF] {}분봉 ADX({})={:.1f} +DI={:.1f} -DI={:.1f} → {}",
                                symbol, _tf_min, _adx_per, _last_adx, _last_dip, _last_dim,
                                "하락▼(차단)" if _htf_is_down else "상승▲(통과)",
                            )
                        else:
                            _htf_trend_cache[symbol] = (_bar_key, False)
                    else:
                        _htf_is_down = _cached[1]
                except Exception as _htf_exc:
                    logger.debug("{}: HTF 계산 실패: {}", symbol, _htf_exc)
                    _htf_is_down = False

            # 설정을 통해 전략에 흘려보내기
            _orig_stop = settings.trade_stop_loss_pct
            settings.trade_stop_loss_pct = effective_stop_pct
            # 종목별 EnsembleConfig 없으면 생성 (st_last_direction 틱 간 유지)
            if symbol not in _ensemble_cfgs:
                _ensemble_cfgs[symbol] = EnsembleConfig()
            try:
                decision = decide_from_settings(
                    closes,
                    position_qty=qty,
                    avg_price=avg,
                    news_sentiment=news_score if news_count > 0 else None,
                    news_article_count=news_count,
                    news_critical_count=news_critical,
                    news_strong_neg_count=news_strong_neg,
                    ohlcv_df=ohlcv_df,
                    ohlcv_df_hist=ohlcv_df_hist,
                    entry_date=entry_date,
                    prev_day_high=prev_day_high,
                    prev_day_close=prev_day_close,
                    ensemble_cfg=_ensemble_cfgs[symbol],
                )
            finally:
                settings.trade_stop_loss_pct = _orig_stop
            # 로그용으로 실제 사용된 stop_loss 값을 meta 에 저장 (settings 복원 후 표시용)
            if decision.meta is not None:
                decision.meta["effective_stop_pct"] = round(effective_stop_pct, 2)
                if _atr_val_meta is not None and _last_price_meta:
                    decision.meta["atr14_value"] = round(_atr_val_meta, 2)
                    decision.meta["computed_stop_price"] = round(
                        _last_price_meta * (1 - effective_stop_pct / 100), 0
                    )

            # ── 베어장 신규 미진입 게이트 (일봉 지수 레짐) ──────────────────────
            # 종목이 속한 시장지수가 MA아래 & 모멘텀− 면 신규 BUY 차단.
            # (기간 = settings.regime_ma_period / regime_mom_days 파라미터)
            # 포지션 없을 때 BUY 만 차단(매도/손절/익절 정상).
            # ※ settings.symbols 는 suffix 없는 6자리 코드라 endswith(".KQ") 가
            #   항상 False → 코스닥 종목이 코스피로 오판되던 버그(2026-07-02 테스).
            #   suffix 있으면 그대로, 없으면 네이버 시장구분 조회(캐시)로 판별.
            if (settings.regime_block_enabled and decision.signal is MACrossSignal.BUY
                    and qty == 0):
                if symbol.endswith(".KQ"):
                    _mkt = "KOSDAQ"
                elif symbol.endswith(".KS"):
                    _mkt = "KOSPI"
                else:
                    _mkt = naver_index.stock_market(symbol) or "KOSPI"
                if naver_index.regime_blocks(
                    _mkt, ma_period=settings.regime_ma_period,
                    mom_days=settings.regime_mom_days,
                ):
                    logger.info(
                        "{} [레짐-차단] {} 일봉 {}MA아래 & {}일모멘텀− → 신규 매수 차단",
                        symbol, _mkt, settings.regime_ma_period, settings.regime_mom_days,
                    )
                    # meta 보존 — 차단돼도 평소처럼 지표 상세(VWAP/ST/RSI/BB…)를 그대로
                    #   찍기 위함(close-block 과 동형). 신호만 HOLD 로 바꿔 매수를 막는다.
                    decision = Decision(
                        MACrossSignal.HOLD, "bear-regime-block",
                        meta={**(decision.meta or {}), "decision": "bear_regime_block"},
                    )

            # ── 종목 일봉 게이트 (개별 종목 하락추세 시 신규 미진입) ───────────────
            # 그 종목 자신의 일봉이 MA아래 & MA 가파른 하락이면 신규 BUY 차단 (MA=STOCK_DAILY_GATE_MA).
            # 지수 레짐(시장 전체)과 별개. 포지션 없을 때 BUY 만 차단(매도/손절/익절 정상).
            if (settings.stock_daily_gate_enabled and decision.signal is MACrossSignal.BUY
                    and qty == 0):
                if _stock_daily_gate_down(broker, symbol):
                    logger.info(
                        "{} [종목게이트-차단] 일봉 {}MA아래 & {}일 기울기 {}%↓ → 신규 매수 차단",
                        symbol, settings.stock_daily_gate_ma,
                        settings.stock_daily_gate_slope_days, settings.stock_daily_gate_slope_pct,
                    )
                    # meta 보존 — 차단돼도 지표 상세를 평소처럼 출력(위 레짐-차단과 동형).
                    decision = Decision(
                        MACrossSignal.HOLD, "stock-daily-gate-block",
                        meta={**(decision.meta or {}), "decision": "stock_daily_gate_block"},
                    )

            # ── HTF 하락추세 시 신규 매수 완전 차단 ──────────────────────────────
            # 포지션 없을 때 BUY 신호만 차단 (매도/손절은 정상 동작)
            # 예외(MA 오버라이드): 현재가가 5분봉 EMA 근접 → 지지 반등 포착, 차단 해제
            if _htf_is_down and decision.signal is MACrossSignal.BUY and qty == 0:
                _ma_override = False
                # EMA120 은 종가 기반 → 어제 워밍업이 포함된 closes 사용 (오늘 실봉만으론 9:40 EMA120 미완성)
                if settings.htf_ma_override_enabled and len(closes) >= 20:
                    _hist_close = closes
                    _n          = len(_hist_close)
                    _req_span   = settings.htf_ma_override_span
                    # 봉 수 부족 시 절반씩 fallback (120→60→20)
                    if _n >= _req_span:
                        _ma_span = _req_span
                    elif _n >= _req_span // 2:
                        _ma_span = _req_span // 2
                    else:
                        _ma_span = 20
                    _ma_val   = float(_hist_close.ewm(span=_ma_span, adjust=False).mean().iloc[-1])
                    _cur_p    = float(closes.iloc[-1])
                    _dist_pct = abs(_cur_p - _ma_val) / _ma_val * 100
                    if _dist_pct <= settings.htf_ma_override_pct:
                        _ma_override = True
                        logger.info(
                            "{} [HTF-MA오버라이드] EMA{} 근접 ({:.2f}% <= {:.1f}%, 현재 {:,.0f} / MA {:,.0f}) -> 차단 해제",
                            symbol, _ma_span, _dist_pct, settings.htf_ma_override_pct, _cur_p, _ma_val,
                        )

                if not _ma_override:
                    logger.info(
                        "{} [HTF-차단] {}분봉 ADX({})>{} AND -DI>+DI 하락추세 -> 신규 매수 차단",
                        symbol, settings.htf_block_tf_minutes, settings.htf_block_adx_period, settings.htf_block_adx_threshold,
                    )
                    # meta 보존 — 차단돼도 지표 상세를 평소처럼 출력(위 레짐-차단과 동형).
                    decision = Decision(
                        MACrossSignal.HOLD, "htf-downtrend-block",
                        meta={**(decision.meta or {}), "decision": "htf_downtrend_block"},
                    )

            # ── 시간대 처리 (09:00~09:40 장초반 변동성 대응) ──────────────────
            # 1) 수익 ≥ N% + 오늘 강제매도 미실행 → 분할 강제매도 (이익 즉시 확정)
            # 2) BUY (신규/추가매수) → 차단 (장초반 진입 위험 회피)
            # 3) SELL (일반/stop_loss) → 모두 통과 (앙상블 결정 신뢰)
            if settings.entry_block_enabled:
                _now_time = datetime.now(tz=_KST).time()
                try:
                    _bs = dtime.fromisoformat(settings.entry_block_start)
                    _be = dtime.fromisoformat(settings.entry_block_end)
                    if _bs <= _now_time < _be:
                        _last_p = float(closes.iloc[-1])
                        _profit_pct = (_last_p - avg) / avg * 100 if (qty > 0 and avg > 0) else 0.0
                        _min_p = settings.entry_block_min_profit_to_sell_pct

                        # (1) 보유 + 수익 충분 + 오늘 아직 강제매도 안 함 → 분할 강제매도
                        if (qty > 0 and avg > 0 and _profit_pct >= _min_p
                                and decision.signal != MACrossSignal.SELL
                                and not _has_force_sold_today(symbol)):
                            _frac = settings.entry_block_force_sell_fraction
                            logger.info(
                                "{} [entry-block] 강제매도 (수익 {:+.2f}% ≥ {:.1f}%, {:.0%} 매도): {} → SELL",
                                symbol, _profit_pct, _min_p, _frac, decision.signal.value,
                            )
                            decision = Decision(
                                MACrossSignal.SELL,
                                f"entry-block 강제매도 {_frac:.0%} (수익 {_profit_pct:+.2f}% ≥ {_min_p:.1f}%)",
                                meta={
                                    **(decision.meta or {}),
                                    "kind": "entry_block_force_sell",
                                    "decision": "entry_block_force_sell",
                                    "sell_fraction": _frac,
                                    "profit_pct": round(_profit_pct, 2),
                                    "last_price": _last_p,
                                    "avg_price": avg,
                                },
                            )
                        # (2) BUY 신호 → HOLD 로 변환 (신규/추가매수 모두 차단)
                        elif decision.signal == MACrossSignal.BUY:
                            logger.info(
                                "{} [entry-block] BUY 차단 ({}~{} 장초반)",
                                symbol, settings.entry_block_start, settings.entry_block_end,
                            )
                            decision = Decision(
                                MACrossSignal.HOLD,
                                "entry-block BUY 차단",
                                meta={**(decision.meta or {}), "decision": "entry_blocked"},
                            )
                        # (3) SELL — stop_loss는 통과, 앙상블 SELL은 장초반 차단 (워밍업 미완료)
                        elif decision.signal == MACrossSignal.SELL and qty > 0:
                            _kind = (decision.meta or {}).get("kind", "")
                            if _kind == "stop_loss":
                                logger.info(
                                    "{} [entry-block] SELL 통과 (stop_loss, 손실 {:+.2f}%)",
                                    symbol, _profit_pct,
                                )
                            else:
                                logger.info(
                                    "{} [entry-block] SELL 차단 ({}~{} 장초반, kind={})",
                                    symbol, settings.entry_block_start, settings.entry_block_end,
                                    _kind or "ensemble",
                                )
                                decision = Decision(
                                    MACrossSignal.HOLD,
                                    "entry-block SELL 차단",
                                    meta={**(decision.meta or {}), "decision": "entry_blocked_sell"},
                                )
                except Exception as exc:
                    logger.warning("entry_block parse error: {}", exc)

            # ── 장마감 전 BUY 차단 (예: 15:00~ 마감 30분 전부터) ──────────────
            # SELL/stop_loss 는 모두 통과 (보유 종목 청산은 허용)
            if settings.close_block_enabled:
                try:
                    _now_time = datetime.now(tz=_KST).time()
                    _cs = dtime.fromisoformat(settings.close_block_start)
                    if _now_time >= _cs and decision.signal == MACrossSignal.BUY:
                        logger.info(
                            "{} [close-block] BUY 차단 ({} 이후 장마감 임박)",
                            symbol, settings.close_block_start,
                        )
                        decision = Decision(
                            MACrossSignal.HOLD,
                            f"close-block BUY 차단 ({settings.close_block_start}~)",
                            meta={**(decision.meta or {}), "decision": "close_blocked"},
                        )
                except Exception as exc:
                    logger.warning("close_block parse error: {}", exc)

            # ── 분할 익절 (take-profit partial sell) ──────────────────────────
            # 앙상블 신호와 무관하게 수익률이 take_profit_pct 이상이면
            # take_profit_fraction 만큼 부분 매도 (하루 1회 제한)
            if (settings.take_profit_enabled and qty > 0 and avg > 0
                    and decision.signal is not MACrossSignal.SELL):
                _tp_price = float(closes.iloc[-1])
                _tp_profit_pct = (_tp_price - avg) / avg * 100
                _tp_today = datetime.now(tz=_KST).strftime("%Y-%m-%d")
                _tp_fired_date = _take_profit_fired.get(symbol)
                if _tp_profit_pct >= settings.take_profit_pct and _tp_fired_date != _tp_today:
                    _tp_frac = settings.take_profit_fraction
                    logger.info(
                        "{} [take-profit] 수익 {:+.2f}% >= {:.1f}%, {:.0%} 분할익절",
                        symbol, _tp_profit_pct, settings.take_profit_pct, _tp_frac,
                    )
                    decision = Decision(
                        MACrossSignal.SELL,
                        f"take-profit 분할익절 {_tp_frac:.0%} (수익 {_tp_profit_pct:+.2f}% >= {settings.take_profit_pct:.1f}%)",
                        meta={
                            **(decision.meta or {}),
                            "kind": "take_profit",
                            "decision": "take_profit",
                            "sell_fraction": _tp_frac,
                            "profit_pct": round(_tp_profit_pct, 2),
                            "last_price": _tp_price,
                            "avg_price": avg,
                        },
                    )
                    _take_profit_fired[symbol] = _tp_today

            if settings.trade_strategy == "ensemble" and decision.meta:
                logger.info("{}", _build_tick_log(
                    symbol, decision, closes, ohlcv_df,
                    ohlcv_df_hist=ohlcv_df_hist,
                ))
            else:
                logger.info(
                    "{} [{}]: {} ({})",
                    symbol, settings.trade_strategy,
                    decision.signal.value, decision.reason,
                )
            metrics.last_price.labels(symbol=symbol).set(float(closes.iloc[-1]))
            metrics.position_qty.labels(symbol=symbol).set(qty)
            metrics.position_avg_price.labels(symbol=symbol).set(avg)
            mode = "dry_run" if settings.trade_dry_run else settings.kis_env

            # 거래 시 저장할 공통 컨텍스트
            _trade_ts = _utcnow()
            trade_context = {
                "meta": decision.meta,
                "news": {
                    "score": news_score,
                    "article_count": news_count,
                    "critical_count": news_critical,
                    "lookback_hours": settings.news_lookback_hours,
                    "articles": recent_news_articles(
                        symbol, _trade_ts, hours=settings.news_lookback_hours, limit=5
                    ),
                },
                "stop_loss_pct": effective_stop_pct,
                "candle": settings.live_candle,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "avg_price": avg,  # 매도 시 수익률 계산용 평단가
            }

            if decision.signal is MACrossSignal.BUY:
                # ── 손절 후 쿨다운 체크 ─────────────────────────────────
                _in_cd, _cd_left = _is_in_stop_loss_cooldown(symbol)
                if _in_cd:
                    logger.info(
                        "{}: 손절 후 쿨다운 중 (잔여 {:.1f}분 / 총 {}분), BUY skip",
                        symbol, _cd_left, settings.post_stoploss_cooldown_min,
                    )
                    continue
                price = float(closes.iloc[-1])
                account_value = _get_account_value(broker)
                is_add_buy = decision.meta.get("kind") == "add_buy"

                cur_pos_pct = (qty * price) / account_value if account_value > 0 else 0.0
                if is_add_buy:
                    # ── 추가매수: 한도 및 포지션 비율 확인 ────────────────
                    add_count = _get_add_buy_count(symbol)
                    if add_count >= settings.add_buy_max_count:
                        logger.info(
                            "{}: 추가매수 한도 초과 (오늘 {}회 / 최대 {}회), skip",
                            symbol, add_count, settings.add_buy_max_count,
                        )
                        continue
                    if cur_pos_pct >= settings.add_buy_max_position_pct:
                        logger.info(
                            "{}: 추가매수 포지션 한도 초과 ({:.1f}% >= {:.0f}%), skip",
                            symbol, cur_pos_pct * 100, settings.add_buy_max_position_pct * 100,
                        )
                        continue
                    # 추가매수 전용 사이징 (add_buy_fraction)
                    _orig_fraction = settings.position_fraction
                    settings.position_fraction = settings.add_buy_fraction
                    try:
                        sizing = _compute_sizing(price, ohlcv, account_value, atr_override=_atr_value)
                    finally:
                        settings.position_fraction = _orig_fraction
                else:
                    # ── 신규매수: 기본 사이징 ──────────────────────────────
                    sizing = _compute_sizing(price, ohlcv, account_value, atr_override=_atr_value)

                if sizing.quantity <= 0:
                    logger.warning("{}: sizing skipped ({})", symbol, sizing.note)
                    continue

                # 점유 선점: 대장주가 같은 봉에 먼저 잡았으면 양보(더블 매수 방지).
                # 이미 보유 중(추가매수)이면 내 소유라 claim True.
                if (settings.leader_own_symbol_priority
                        and not position_owner.claim(symbol, "stock", sizing.quantity)):
                    logger.info("{} [점유-양보] 대장주가 선점 → 신규매수 skip", symbol)
                    continue

                resp = broker.place_order(symbol, "buy", sizing.quantity)
                reason = _build_narrative(decision, "buy")
                # 실제 체결가 조회 (KIS 평단가 업데이트까지 잠시 대기)
                # 시장가 주문이라 신호가(price)와 체결가가 다를 수 있음
                import time as _t
                _t.sleep(1.5)
                exec_price = price  # fallback (KIS 조회 실패 시 신호가 사용)
                try:
                    for _pos_row in broker.get_positions():
                        if _pos_row.get("pdno") == symbol.split(".")[0]:
                            _fill = float(_pos_row.get("pchs_avg_pric", 0) or 0)
                            if _fill > 0:
                                exec_price = _fill
                                if abs(exec_price - price) >= 1:
                                    logger.info(
                                        "{} 체결가 확인: {:,.0f}원 (신호가 {:,.0f}원, 차이 {:+,.0f}원)",
                                        symbol, exec_price, price, exec_price - price,
                                    )
                            break
                except Exception as _exc:
                    logger.warning("{}: 체결가 조회 실패, 신호가 사용: {}", symbol, _exc)

                trade_context["sizing"] = {
                    "method": sizing.method,
                    "quantity": sizing.quantity,
                    "note": sizing.note,
                    "account_value": account_value,
                    "add_buy": is_add_buy,
                }
                trade_context["signal_price"] = price       # 신호 시점 가격 (참고용)
                trade_context["exec_price"] = exec_price    # 실제 체결가 (KIS 평단가 기준)
                record_trade(
                    symbol, "buy", sizing.quantity, exec_price, reason, json.dumps(resp, ensure_ascii=False),
                    strategy=settings.trade_strategy, details=trade_context,
                )
                if is_add_buy:
                    _increment_add_buy(symbol)
                metrics.orders_total.labels(symbol=symbol, side="buy", mode=mode).inc()
                _nm = get_name(symbol)
                _arts = trade_context["news"].get("articles", [])
                _art_text = (
                    "\n\n📰 관련 뉴스\n"
                    + "\n".join(
                        f"{'★' if a['is_critical'] else '·'} [{a['score']:+.1f}] {a['title'][:45]}{'…' if len(a['title']) > 45 else ''}"
                        for a in _arts[:3]
                    )
                ) if _arts else ""
                _buy_label = "🟠 **추가매수**" if is_add_buy else "🔴 **매수**"
                _add_note = (
                    f"추가매수 {_get_add_buy_count(symbol)}/{settings.add_buy_max_count}회 "
                    f"(현포지션 {cur_pos_pct*100:.1f}%→추가 {settings.add_buy_fraction*100:.0f}%)\n"
                    if is_add_buy else
                    f"사이징: {sizing.method} ({sizing.note})\n"
                )
                notify(
                    f"{_buy_label} {symbol}{f' ({_nm})' if _nm else ''} {sizing.quantity}주 @ {exec_price:,.0f}원\n"
                    + _add_note
                    + f"시간: {_now_kst()}\n\n"
                    + reason
                    + _art_text
                )

            elif decision.signal is MACrossSignal.SELL and qty > 0:
                price = float(closes.iloc[-1])
                sell_reason = _build_narrative(decision, "sell")
                # 분할매도: meta.sell_fraction 있으면 일부만 매도 (entry_block_force_sell 등)
                _sell_frac = (decision.meta or {}).get("sell_fraction", 1.0)
                _sell_qty = qty
                if 0.0 < _sell_frac < 1.0:
                    _sell_qty = max(1, int(qty * _sell_frac))
                    logger.info(
                        "{} 분할매도: 보유 {}주 × {:.0%} = {}주 매도 (잔량 {}주)",
                        symbol, qty, _sell_frac, _sell_qty, qty - _sell_qty,
                    )
                _sell_kind = (decision.meta or {}).get("kind")
                # 즉시 체결 필요 여부: 손절/강제매도/뉴스 긴급매도는 지연 불가
                _immediate_sell_kinds = {"stop_loss", "entry_block_force_sell", "news_critical_sell", "take_profit"}
                _is_immediate = _sell_kind in _immediate_sell_kinds
                # sell_on_next_open=False 면 모든 일반매도도 즉시 체결
                if not settings.sell_on_next_open:
                    _is_immediate = True

                if not _is_immediate:
                    # 일반 앙상블 매도 → 다음 봉 시가 지연 체결
                    _pending_sell[symbol] = {
                        "decision": decision,
                        "sell_qty": _sell_qty,
                        "avg_price": avg,
                        "signal_price": price,
                        # "KST yyyy-mm-dd HH:MM:SS" 로 기록 — 체결시각/로그가 모두
                        # KST 라 접두어로 명시(표시/리뷰용, 계산엔 미사용).
                        "signal_ts": "KST " + _utcnow().replace(tzinfo=timezone.utc)
                        .astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S"),
                        "trade_context": trade_context,
                    }
                    _nm = get_name(symbol)
                    _pnl_pct = ((price - avg) / avg * 100) if avg > 0 else 0.0
                    _pnl_str = f"{'▲' if _pnl_pct >= 0 else '▼'} {_pnl_pct:+.2f}%"
                    logger.info(
                        "{} 매도신호 → 지연 큐 등록 ({}주, 다음 봉 시가 체결 예정, 현재 {:+.2f}%)",
                        symbol, _sell_qty, _pnl_pct,
                    )
                    notify(
                        f"⏳ **매도대기** {symbol}{f' ({_nm})' if _nm else ''} {_sell_qty}주 (다음 봉 시가 체결 예정)\n"
                        f"현재가: {price:,.0f}원 | {_pnl_str} (평단 {avg:,.0f}원)\n"
                        f"이유: {sell_reason}\n"
                        f"시간: {_now_kst()}"
                    )
                else:
                    # 손절 / 강제매도 → 즉시 체결
                    resp = broker.place_order(symbol, "sell", _sell_qty)
                    if _sell_kind == "entry_block_force_sell":
                        _mark_force_sold(symbol)
                    if _sell_kind == "stop_loss":
                        _mark_stop_loss(symbol)
                    # 실제 체결가 조회 (시장가 주문 후 현재가 = 체결가 근사값)
                    # 매도는 pchs_avg_pric(매수 평단) 이 안 맞으므로 KIS 현재가 호출.
                    exec_price = price  # fallback (조회 실패 시 신호가)
                    try:
                        import time as _t
                        _t.sleep(1.0)
                        _q = broker.get_quote(symbol)
                        if _q and float(_q.price) > 0:
                            exec_price = float(_q.price)
                            if abs(exec_price - price) >= 1:
                                logger.info(
                                    "{} 매도 체결가 확인: {:,.0f}원 (신호가 {:,.0f}원, 차이 {:+,.0f}원)",
                                    symbol, exec_price, price, exec_price - price,
                                )
                    except Exception as _exc:
                        logger.warning("{}: 매도 체결가 조회 실패, 신호가 사용: {}", symbol, _exc)

                    trade_context["signal_price"] = price       # 신호 시점 가격
                    trade_context["exec_price"] = exec_price    # 실제 체결가
                    record_trade(
                        symbol, "sell", _sell_qty, exec_price, sell_reason, json.dumps(resp, ensure_ascii=False),
                        strategy=settings.trade_strategy, details=trade_context,
                    )
                    metrics.orders_total.labels(symbol=symbol, side="sell", mode=mode).inc()
                    _nm = get_name(symbol)
                    _arts = trade_context["news"].get("articles", [])
                    _art_text = (
                        "\n\n📰 관련 뉴스\n"
                        + "\n".join(
                            f"{'★' if a['is_critical'] else '·'} [{a['score']:+.1f}] {a['title'][:45]}{'…' if len(a['title']) > 45 else ''}"
                            for a in _arts[:3]
                        )
                    ) if _arts else ""
                    _pnl_pct = ((exec_price - avg) / avg * 100) if avg > 0 else 0.0
                    _pnl_str = f"{'▲' if _pnl_pct >= 0 else '▼'} {_pnl_pct:+.2f}%"
                    _partial_note = (
                        f" ({_sell_qty}/{qty}주 분할매도, 잔량 {qty - _sell_qty}주)"
                        if _sell_qty < qty else ""
                    )
                    _sell_label = "🟠 **분할매도**" if _sell_qty < qty else "🔵 **매도**"
                    notify(
                        f"{_sell_label} {symbol}{f' ({_nm})' if _nm else ''} {_sell_qty}주 @ {exec_price:,.0f}원{_partial_note}\n"
                        f"수익률: {_pnl_str} (평단 {avg:,.0f}원)\n"
                        f"시간: {_now_kst()}\n\n"
                        + sell_reason
                        + _art_text
                    )

        except Exception as exc:
            logger.exception("tick failed for {}: {}", symbol, exc)
            metrics.tick_errors_total.labels(symbol=symbol).inc()
            _nm = get_name(symbol)
            notify(f"⚠️ **오류** {symbol}{f' ({_nm})' if _nm else ''}: {exc}")


def _start_env_watcher(scope: str = "all") -> None:
    """백그라운드 스레드로 1초마다 .env 변경 감시 → 즉시 핫리로드.

    scope: "stock"·"leader"·"all" — 이 프로세스가 반영할 키 범위(자기 봇 키만).
    """
    import threading
    import time as _time

    def _loop() -> None:
        while True:
            try:
                _reload_env_if_changed(scope)
            except Exception as exc:
                logger.debug("env watcher error: {}", exc)
            _time.sleep(1.0)

    t = threading.Thread(target=_loop, name="env-watcher", daemon=True)
    t.start()
    logger.info("env watcher started (1s poll, scope={})", _SCOPE_LABEL.get(scope, scope))


def run_live(interval_minutes: int | None = None) -> None:
    init_db()
    init_news_db()
    init_costs_db()
    metrics.start_metrics_server()
    _start_env_watcher("stock")
    broker = KISBroker()
    global _holiday_broker
    _holiday_broker = broker  # 휴장일 조회를 KIS 달력 기준으로
    interval = interval_minutes or settings.live_interval_minutes
    mode = "시뮬레이션" if settings.trade_dry_run else ("실전" if settings.kis_env == "real" else "모의투자")
    sym_list = ", ".join(
        f"{s}{f' ({get_name(s)})' if get_name(s) else ''}" for s in settings.symbols
    )
    notify(
        f"🤖 **스톡봇 기동** [{mode}]\n"
        f"전략: {settings.trade_strategy} · 종목: {sym_list}"
    )
    logger.info("live runner started, mode={} interval={}min", mode, interval)

    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    def _tick_if_trading_day():
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now) or not _is_market_open(now):
            return
        _tick(broker)

    scheduler.add_job(
        _tick_if_trading_day,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute=f"*/{interval}",
        ),
        id="trade_tick",
    )
    if settings.news_enabled:
        # 장중: 5분마다 크롤 + critical 즉시 tick (공휴일 제외)
        def _news_tick_intraday():
            now = datetime.now(tz=_KST)
            if not _is_trading_day(now) or not _is_market_open(now):
                return
            _news_tick(broker)

        scheduler.add_job(
            _news_tick_intraday,
            CronTrigger(
                day_of_week="mon-fri",
                hour="9-15",
                minute="*/5",
            ),
            id="news_tick_intraday",
            max_instances=1,
            coalesce=True,
        )
        # 장외 + 주말: 저빈도로 유지해 오버나이트/주말 뉴스도 수집 (장중 9-15시 평일 제외)
        _interval = settings.news_crawl_interval_minutes
        _min = "0" if _interval >= 60 else f"*/{_interval}"
        # 평일 장외 (9-15시 제외)
        def _news_tick_offhours_weekday():
            now = datetime.now(tz=_KST)
            # 거래일 9:00~15:30은 intraday가 담당 → 스킵
            if _is_trading_day(now) and dtime(9, 0) <= now.time() <= dtime(15, 30):
                return
            _news_tick(None)

        scheduler.add_job(
            _news_tick_offhours_weekday,
            CronTrigger(day_of_week="mon-fri", minute=_min),
            id="news_tick_offhours_weekday",
            next_run_time=datetime.now(),
            max_instances=1,
            coalesce=True,
        )
        # 주말 전일
        scheduler.add_job(
            _news_tick,
            CronTrigger(day_of_week="sat,sun", minute=_min),
            args=[None],
            id="news_tick_offhours_weekend",
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "news crawl: 5min intraday (critical→instant tick), {}min off-hours (llm={})",
            settings.news_crawl_interval_minutes,
            settings.news_prefer_llm,
        )

    # ── 대장주 선별·매매는 별도 leader-bot 컨테이너로 분리 ──────────────
    # (stock_bot/live/leader_runner.py — 이 프로세스 장애가 메인 봇에 영향 없도록)

    # 장마감 리뷰: 평일 15:35 KST 에 당일 거래를 Claude 로 리뷰
    scheduler.add_job(
        run_daily_review,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=35),
        id="daily_review",
        max_instances=1,
        coalesce=True,
    )
    logger.info("daily review scheduled: mon-fri 15:35 KST")

    # API 비용 리포트: 매일 자정 KST
    scheduler.add_job(
        _send_cost_report,
        CronTrigger(hour=0, minute=0),
        id="cost_report",
        max_instances=1,
        coalesce=True,
    )
    logger.info("cost report scheduled: daily 00:00 KST")

    # 일별 DB 백업: 매일 00:05 KST (CSV → git push)
    scheduler.add_job(
        run_backup,
        CronTrigger(hour=0, minute=5),
        id="daily_backup",
        max_instances=1,
        coalesce=True,
    )
    logger.info("daily backup scheduled: daily 00:05 KST")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutting down")
    finally:
        broker.close()


if __name__ == "__main__":
    run_live()
