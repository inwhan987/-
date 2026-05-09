"""실시간 거래 러너.

장중 1분마다 실행하지는 않고, 기본 15분 주기로 일봉 데이터를 당겨 시그널을 계산한다.
KRX 정규장 (09:00 ~ 15:30 KST) 에만 동작.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

_KST = timezone(timedelta(hours=9))


def _now_kst() -> str:
    return datetime.now(tz=_KST).strftime("%Y-%m-%d %H:%M KST")

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from stock_bot.broker import KISBroker
from stock_bot.config import settings
from stock_bot.indicators import atr_from_ohlcv
from stock_bot.live.backup import run_backup
from stock_bot.live.review import run_daily_review
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

# 추가매수 일별 카운터 (메모리, 자정 KST 기준 자동 리셋)
_add_buy_count: dict[str, int] = {}
_add_buy_date: dict[str, str] = {}

# 종목별 EnsembleConfig 유지 (st_last_direction 등 틱 간 상태 보존)
_ensemble_cfgs: dict[str, EnsembleConfig] = {}


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
    ("TRADE_RSI_PERIOD", "trade_rsi_period", int),
    ("TRADE_RSI_OVERSOLD", "trade_rsi_oversold", float),
    ("TRADE_RSI_OVERBOUGHT", "trade_rsi_overbought", float),
    ("TRADE_VWAP_WARMUP_BARS", "trade_vwap_warmup_bars", int),
    ("TRADE_CASH_PER_TRADE", "trade_cash_per_trade", int),
    ("LIVE_INTERVAL_MINUTES", "live_interval_minutes", int),
    ("LIVE_CANDLE", "live_candle", str),
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
    ("ADD_BUY_ENABLED", "add_buy_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("ADD_BUY_THRESHOLD", "add_buy_threshold", float),
    ("ADD_BUY_MIN_VOTES", "add_buy_min_votes", int),
    ("ADD_BUY_MAX_COUNT", "add_buy_max_count", int),
    ("ADD_BUY_FRACTION", "add_buy_fraction", float),
    ("ADD_BUY_MAX_POSITION_PCT", "add_buy_max_position_pct", float),
)


def _reload_env_if_changed() -> None:
    """`.env` / `.env.overrides` 변경 감지 → 핫리로드.

    도커에서 env vars 가 os.environ 에 고정되므로 pydantic Settings 재인스턴스화로는
    갱신되지 않는다. 파일을 직접 파싱해 `settings` 객체 속성을 덮어쓴다.
    우선순위: .env.overrides > .env
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
        if key not in parsed:
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
        logger.info(".env 변경 감지, 핫리로드: {}", "; ".join(changed))


_STRATEGY_KO = {
    "vwap": "VWAP",
    "supertrend": "Supertrend",
    "rsi": "RSI",
    "bollinger": "볼린저",
    "ema": "EMA크로스",
    "macd": "MACD",
    "momentum": "모멘텀",
    "daily_context": "장기보유청산",
}


def _build_tick_log(
    symbol: str,
    decision,
    closes: pd.Series,
    ohlcv_df: pd.DataFrame | None,
) -> str:
    """전략별 실제 수치를 포함한 상세 틱 로그 한 줄 생성."""
    import re
    from stock_bot.strategy.rsi import _rsi

    meta = decision.meta
    votes = {v["name"]: v for v in meta.get("votes", [])}
    last = float(closes.iloc[-1])
    score = meta.get("weighted_score", 0)
    bv = meta.get("buy_votes", 0)
    sv = meta.get("sell_votes", 0)
    sig = decision.signal.value.upper()

    _SIG = {"buy": "▲매수", "sell": "▼매도", "hold": "─홀드"}

    parts: list[str] = []

    # ── VWAP ─────────────────────────────────────────────────────────
    if ohlcv_df is not None and len(ohlcv_df) >= 5:
        try:
            tp = (ohlcv_df["high"] + ohlcv_df["low"] + ohlcv_df["close"]) / 3
            vol = ohlcv_df["volume"].replace(0, 1)
            vwap = float((tp * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1])
            dev = (last - vwap) / vwap * 100
            vsig = _SIG.get(votes.get("vwap", {}).get("signal", "hold"), "─홀드")
            parts.append(f"VWAP {vwap:,.0f}원 {dev:+.2f}% {vsig}")
        except Exception:
            pass

    # ── Supertrend ────────────────────────────────────────────────────
    st_v = votes.get("supertrend", {})
    st_reason = st_v.get("reason", "")
    if "상승 전환" in st_reason:
        st_state = "하락→상승전환"
    elif "하락 전환" in st_reason:
        st_state = "상승→하락전환"
    elif "상승추세" in st_reason or "상승" in st_reason:
        st_state = "상승추세"
    elif "하락추세" in st_reason or "하락" in st_reason:
        st_state = "하락추세"
    else:
        st_state = "중립"
    vsig = _SIG.get(st_v.get("signal", "hold"), "─홀드")
    parts.append(f"ST {st_state} {vsig}")

    # ── RSI ───────────────────────────────────────────────────────────
    try:
        rsi_val = float(_rsi(closes, settings.trade_rsi_period).iloc[-1])
        vsig = _SIG.get(votes.get("rsi", {}).get("signal", "hold"), "─홀드")
        parts.append(
            f"RSI {rsi_val:.1f} "
            f"(기준 {settings.trade_rsi_oversold:.0f}/{settings.trade_rsi_overbought:.0f}) "
            f"{vsig}"
        )
    except Exception:
        pass

    # ── Bollinger ─────────────────────────────────────────────────────
    try:
        bb_mid = float(closes.rolling(settings.trade_bb_window).mean().iloc[-1])
        bb_std = float(closes.rolling(settings.trade_bb_window).std().iloc[-1])
        bb_upper = bb_mid + settings.trade_bb_k * bb_std
        bb_lower = bb_mid - settings.trade_bb_k * bb_std
        vsig = _SIG.get(votes.get("bollinger", {}).get("signal", "hold"), "─홀드")
        parts.append(f"BB {bb_lower:,.0f}~{bb_upper:,.0f}원 {vsig}")
    except Exception:
        pass

    # ── DailyContext ──────────────────────────────────────────────────
    dc_v = votes.get("daily_context", {})
    dc_reason = dc_v.get("reason", "")
    dc_sig = dc_v.get("signal", "hold")
    if "gate1" in dc_reason:
        dc_str = "DC 당일진입(제외)"
    elif "gate2" in dc_reason:
        m = re.search(r"수익[=]?([+-]?[\d.]+)%", dc_reason)
        pct = m.group(1) if m else "?"
        dc_str = f"DC 수익{pct}%<{settings.daily_context_profit_gate_pct}%(게이트미달)"
    elif dc_sig == "sell":
        m = re.search(r"수익[=]?([+-]?[\d.]+)%", dc_reason)
        pct = m.group(1) if m else "?"
        dc_str = f"DC 장기보유청산(수익{pct}%) ▼매도"
    else:
        dc_str = "DC ─홀드"
    parts.append(dc_str)

    # ── 뉴스 ─────────────────────────────────────────────────────────
    news_bias = meta.get("news_bias", 0)
    news_n = meta.get("news_article_count", 0)
    if news_n > 0:
        parts.append(f"뉴스 {news_bias:+.3f} ({news_n}건)")

    detail = "\n    ".join(parts)
    # ATR 손절 정보 (활성 시만)
    atr_str = ""
    if settings.atr_stop_loss_enabled or settings.position_sizing == "atr":
        atr_str = f" | 손절 -{settings.trade_stop_loss_pct:.2f}%(ATR)"
    header = (
        f"{symbol} [{settings.trade_strategy}] {sig} "
        f"score={score:+.2f} B{bv}/S{sv}"
        f" | 현재가 {last:,.0f}원{atr_str}"
    )
    return f"{header}\n    {detail}"


def _vote_sentence(name: str, reason: str, signal: str) -> str:
    """전략별 원시 reason → 한국어 한 줄 설명."""
    import re
    if name == "vwap":
        m = re.search(r'([+-][\d.]+)%', reason)
        pct = m.group(1) if m else "?"
        mv = re.search(r'vwap=([\d,]+)', reason)
        ref = mv.group(1) if mv else "?"
        if signal == "buy":
            return f"VWAP 기준({ref}원)보다 {pct}% 하락 이탈 → 평균회귀 매수"
        elif signal == "sell":
            return f"VWAP 기준({ref}원)보다 {pct}% 상승 이탈 → 차익실현"
        return f"VWAP 이탈 없음 (기준 {ref}원)"
    if name == "supertrend":
        if "상승 전환" in reason:
            return "하락→상승 추세 전환 감지 → 매수 신호"
        if "하락 전환" in reason:
            return "상승→하락 추세 전환 감지 → 매도 신호"
        if "상승추세" in reason:
            return "상승추세 유지 중, 진입 조건 미충족"
        if "하락추세" in reason:
            return "하락추세 유지 중, 매도 조건 미충족"
        return reason
    if name == "rsi":
        m = re.search(r'RSI\s*([\d.]+)', reason)
        val = float(m.group(1)) if m else None
        if signal == "buy" and val:
            return f"RSI {val:.1f} — 과매도 기준({settings.trade_rsi_oversold}) 하회, 반등 기대"
        if signal == "sell" and val:
            return f"RSI {val:.1f} — 과매수 기준({settings.trade_rsi_overbought}) 초과, 차익실현"
        return f"RSI {val:.1f} 중립 구간" if val else reason
    if name == "bollinger":
        if "lower rebound" in reason:
            return "볼린저 하단 이탈 후 재진입 → 과매도 반등 신호"
        if "lower turn" in reason:
            return "볼린저 하단 근처에서 2봉 연속 상승 → 반등 신호"
        if "upper revert" in reason:
            return "볼린저 상단 돌파 후 회귀 → 과매수 청산 신호"
        if "upper turn" in reason:
            return "볼린저 상단 근처에서 2봉 연속 하락 → 꺾임 신호"
        return "볼린저 밴드 중간 구간, 신호 없음"
    if name == "daily_context":
        if signal == "sell":
            return f"장기보유 청산 조건 충족 — {reason}"
        if "gate1 실패" in reason:
            return "장기보유 청산 미해당 (당일 진입 포지션)"
        if "gate2 실패" in reason:
            m = re.search(r"수익=([+-]?[\d.]+)%", reason)
            pct = m.group(1) if m else "?"
            return f"수익 {pct}% — 청산 임계(1.5%) 미달"
        if "플로팅 미달" in reason:
            return f"게이트 통과, 가격 조건 미달 ({reason.split('[')[-1].rstrip(']')})"
        return reason
    return reason


def _build_narrative(decision, side: str) -> str:
    """Decision.meta → 한국어 거래 서술문."""
    meta = decision.meta
    kind = meta.get("kind", "")

    if kind == "stop_loss":
        lp = meta.get("loss_pct", 0)
        ap = meta.get("avg_price", 0)
        cp = meta.get("last_price", 0)
        return (
            f"[손절] 평단 {ap:,.0f}원 → 현재 {cp:,.0f}원 ({lp:.2f}%)\n"
            f"손실 한도 초과로 강제 청산"
        )
    if kind == "news_critical_sell":
        ns = meta.get("news_sentiment", 0)
        nc = meta.get("news_critical_count", 0)
        return (
            f"[뉴스 긴급매도] 중요 기사 {nc}건 감지, 감성점수 {ns:+.2f}\n"
            f"포지션 즉시 청산"
        )

    votes = meta.get("votes", [])
    score = meta.get("weighted_score", 0)
    buy_v = meta.get("buy_votes", 0)
    sell_v = meta.get("sell_votes", 0)
    news_bias = meta.get("news_bias", 0)

    if not votes:
        return decision.reason

    lines = []
    for v in votes:
        name = v.get("name", "")
        sig = v.get("signal", "hold")
        raw = v.get("reason", "")
        icon = "✅" if sig == "buy" else "🔴" if sig == "sell" else "⬜"
        label = _STRATEGY_KO.get(name, name.upper())
        lines.append(f"{icon} {label}: {_vote_sentence(name, raw, sig)}")

    sr_adj = meta.get("sr_adj", 0.0)
    sr_tag = meta.get("sr_tag", "")
    if sr_tag:
        icon = "📍" if sr_adj > 0 else "🚧"
        lines.append(f"{icon} S/R: {sr_tag} (점수 {sr_adj:+.2f})")

    summary = f"종합점수 {score:+.2f} | 매수 {buy_v}표 / 매도 {sell_v}표"
    if abs(news_bias) > 0.005:
        direction = "긍정" if news_bias > 0 else "부정"
        summary += f" | 뉴스 {direction} 보정 {news_bias:+.3f}"

    lines.append(f"→ {summary}")
    return "\n".join(lines)




def _send_cost_report() -> None:
    """어제 KST 기준 API 비용 리포트를 Discord로 전송."""
    from datetime import date, timedelta
    from stock_bot.costs import format_daily_report
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        msg = format_daily_report(yesterday)
        notify(msg)
    except Exception as exc:
        logger.warning("비용 리포트 전송 실패: {}", exc)




def _is_trading_day(date_kst: datetime) -> bool:
    """KRX 거래일 여부 (주말 + 공휴일 모두 체크)."""
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XKRX")
        return cal.is_session(date_kst.strftime("%Y-%m-%d"))
    except Exception:
        # 라이브러리 없거나 오류 시 주말만 체크 (폴백)
        return date_kst.weekday() < 5


def _is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=_KST)
    if not _is_trading_day(now):
        return False
    return dtime(9, 0) <= now.time() <= dtime(15, 30)


def _positions_by_symbol(broker: KISBroker) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    for row in broker.get_positions():
        code = row.get("pdno")
        qty = int(row.get("hldg_qty", 0) or 0)
        avg = float(row.get("pchs_avg_pric", 0) or 0)
        if code and qty > 0:
            out[code] = (qty, avg)
    return out


def _get_last_buy_date(symbol: str) -> str | None:
    """TradeLog 에서 해당 종목의 마지막 매수 날짜(KST, "YYYY-MM-DD") 반환."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from stock_bot.storage import ENGINE, TradeLog
    try:
        with Session(ENGINE) as s:
            row = s.scalars(
                select(TradeLog)
                .where(TradeLog.symbol == symbol)
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
    price: float, ohlcv: list[dict], account_value: float
) -> SizingResult:
    mode = settings.position_sizing
    if mode == "fraction":
        return fixed_fraction(account_value, settings.position_fraction, price)
    if mode == "atr":
        atr_value = atr_from_ohlcv(list(reversed(ohlcv)), period=settings.atr_period)
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
    _reload_env_if_changed()
    if not settings.news_enabled:
        return
    trigger_symbols: set[str] = set()
    for symbol in settings.symbols:
        try:
            # DB 최신 기사 시각 기준 10분 여유를 두고 early stop
            last_ts = get_latest_news_ts(symbol)
            since = (last_ts - timedelta(minutes=10)) if last_ts else None
            items = fetch_naver_news(symbol, pages=settings.news_pages_per_symbol, since=since)

            # 1단계: URL·제목 중복 제거 (LLM 호출 전)
            new_items = [
                item for item in items
                if not news_exists(item.symbol, item.url)
                and not news_title_exists(symbol, item.title)
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
    _reload_env_if_changed()
    if not _is_market_open():
        logger.debug("market closed, skip")
        return

    positions = _positions_by_symbol(broker)

    lookback = max(
        settings.trade_long_ma,
        settings.trade_ema_slow,
        settings.trade_rsi_period,
        settings.trade_macd_slow + settings.trade_macd_signal,
        settings.trade_bb_window,
        settings.trade_momentum_period,
    ) + 10

    symbols_to_run = [s for s in settings.symbols if not only_symbols or s in only_symbols]
    for symbol in symbols_to_run:
        try:
            if settings.live_candle == "minute":
                ohlcv = broker.get_minute_ohlcv(
                    symbol, interval_min=settings.live_minute_interval, count=lookback
                )
            else:
                ohlcv = broker.get_daily_ohlcv(symbol, count=lookback)
            # KIS 는 최신이 앞이므로 역순 정렬 (오래된→최신)
            ohlcv_asc = list(reversed(ohlcv))
            closes = pd.Series([row["close"] for row in ohlcv_asc])
            if len(closes) < 3:
                logger.warning("{}: 캔들 데이터 부족 ({}개), skip", symbol, len(closes))
                continue
            # VWAP/Supertrend 용 OHLCV DataFrame (분봉 모드에서만 의미 있음)
            ohlcv_df: pd.DataFrame | None = None
            if settings.live_candle == "minute":
                try:
                    ohlcv_df = pd.DataFrame(ohlcv_asc)[["open", "high", "low", "close", "volume"]]
                    ohlcv_df = ohlcv_df.apply(pd.to_numeric, errors="coerce")
                except Exception:
                    ohlcv_df = None
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
            effective_stop_pct = settings.trade_stop_loss_pct
            if settings.position_sizing == "atr" or settings.atr_stop_loss_enabled:
                atr_val = atr_from_ohlcv(list(reversed(ohlcv)), period=settings.atr_period)
                last_price_tmp = float(closes.iloc[-1])
                if atr_val > 0 and last_price_tmp > 0:
                    dynamic_pct = (atr_val * settings.atr_stop_multiplier) / last_price_tmp * 100
                    effective_stop_pct = dynamic_pct
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
                    entry_date=entry_date,
                    prev_day_high=prev_day_high,
                    prev_day_close=prev_day_close,
                    ensemble_cfg=_ensemble_cfgs[symbol],
                )
            finally:
                settings.trade_stop_loss_pct = _orig_stop
            if settings.trade_strategy == "ensemble" and decision.meta:
                logger.info("{}", _build_tick_log(symbol, decision, closes, ohlcv_df))
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
            _trade_ts = datetime.utcnow()
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
                        sizing = _compute_sizing(price, ohlcv, account_value)
                    finally:
                        settings.position_fraction = _orig_fraction
                else:
                    # ── 신규매수: 기본 사이징 ──────────────────────────────
                    sizing = _compute_sizing(price, ohlcv, account_value)

                if sizing.quantity <= 0:
                    logger.warning("{}: sizing skipped ({})", symbol, sizing.note)
                    continue

                resp = broker.place_order(symbol, "buy", sizing.quantity)
                reason = _build_narrative(decision, "buy")
                trade_context["sizing"] = {
                    "method": sizing.method,
                    "quantity": sizing.quantity,
                    "note": sizing.note,
                    "account_value": account_value,
                    "add_buy": is_add_buy,
                }
                record_trade(
                    symbol, "buy", sizing.quantity, price, reason, str(resp),
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
                    f"{_buy_label} {symbol}{f' ({_nm})' if _nm else ''} {sizing.quantity}주 @ {price:,.0f}원\n"
                    + _add_note
                    + f"시간: {_now_kst()}\n\n"
                    + reason
                    + _art_text
                )

            elif decision.signal is MACrossSignal.SELL and qty > 0:
                price = float(closes.iloc[-1])
                sell_reason = _build_narrative(decision, "sell")
                resp = broker.place_order(symbol, "sell", qty)
                record_trade(
                    symbol, "sell", qty, price, sell_reason, str(resp),
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
                _pnl_pct = ((price - avg) / avg * 100) if avg > 0 else 0.0
                _pnl_str = f"{'▲' if _pnl_pct >= 0 else '▼'} {_pnl_pct:+.2f}%"
                notify(
                    f"🔵 **매도** {symbol}{f' ({_nm})' if _nm else ''} {qty}주 @ {price:,.0f}원\n"
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


def _start_env_watcher() -> None:
    """백그라운드 스레드로 1초마다 .env 변경 감시 → 즉시 핫리로드."""
    import threading
    import time as _time

    def _loop() -> None:
        while True:
            try:
                _reload_env_if_changed()
            except Exception as exc:
                logger.debug("env watcher error: {}", exc)
            _time.sleep(1.0)

    t = threading.Thread(target=_loop, name="env-watcher", daemon=True)
    t.start()
    logger.info("env watcher started (1s poll)")


def run_live(interval_minutes: int | None = None) -> None:
    init_db()
    init_news_db()
    init_costs_db()
    metrics.start_metrics_server()
    _start_env_watcher()
    broker = KISBroker()
    interval = interval_minutes or settings.live_interval_minutes
    mode = "시뮬레이션" if settings.trade_dry_run else ("실전" if settings.kis_env == "real" else "모의투자")
    sym_list = ", ".join(
        f"{s}{f' ({get_name(s)})' if get_name(s) else ''}" for s in settings.symbols
    )
    notify(
        f"🤖 **주식프로그램 기동** [{mode}]\n"
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
