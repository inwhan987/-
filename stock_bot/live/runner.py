"""실시간 거래 러너.

장중 1분마다 실행하지는 않고, 기본 15분 주기로 일봉 데이터를 당겨 시그널을 계산한다.
KRX 정규장 (09:00 ~ 15:30 KST) 에만 동작.
"""
from __future__ import annotations

import json
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

# 종목별 초기 진입 stop_pct 잠금 (포지션 보유 중 stop_pct 고정용)
_locked_stop_pct: dict[str, float] = {}

# 매도 지연: 다음 봉 시가 체결 (일반 앙상블 매도만, 손절/강제매도 제외)
# symbol → {"decision": Decision, "sell_qty": int, "avg_price": float}
_pending_sell: dict[str, dict] = {}


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
    ("HTF_BLOCK_ENABLED",        "htf_block_enabled",        lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("HTF_BLOCK_TF_MINUTES",     "htf_block_tf_minutes",     int),
    ("HTF_BLOCK_EMA_PERIOD",     "htf_block_ema_period",     int),
    ("HTF_MA_OVERRIDE_ENABLED",  "htf_ma_override_enabled",  lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("HTF_MA_OVERRIDE_SPAN",     "htf_ma_override_span",     int),
    ("HTF_MA_OVERRIDE_PCT",      "htf_ma_override_pct",      float),
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
    # 워밍업 봉수만큼 제외 후 VWAP 계산 (전략 로직과 일치)
    # 워밍업 미완료 시 "워밍업 중"만 표시하고 나머지 전략은 생략
    if ohlcv_df is not None:
        _warmup = settings.trade_vwap_warmup_bars
        _df_calc = ohlcv_df.iloc[_warmup:] if len(ohlcv_df) > _warmup else ohlcv_df.iloc[0:0]
        # Phase1: 워밍업 봉수 미달 (9:00~9:40) — VWAP/ST 봉부족, RSI/BB는 히스토리로 계산
        if len(ohlcv_df) < _warmup:
            parts.append(f"VWAP 워밍업 중 ({len(ohlcv_df)}/{_warmup}봉)")
        # Phase2: 워밍업 완료, VWAP 수집 중 (9:40~10:05) — VWAP만 수집중, 나머지는 풀 표시
        if len(_df_calc) < 5:
            parts.append(f"VWAP 수집중 ({len(_df_calc)}/5봉)")
        else:
            try:
                tp = (_df_calc["high"] + _df_calc["low"] + _df_calc["close"]) / 3
                vol = _df_calc["volume"].replace(0, 1)
                vwap = float((tp * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1])
                dev = (last - vwap) / vwap * 100
                vwap_v = votes.get("vwap", {})
                vsig = _SIG.get(vwap_v.get("signal", "hold"), "─홀드")
                contrib = vwap_v.get("contrib", 0.0)
                parts.append(f"VWAP {vwap:,.0f}원 {dev:+.2f}% {vsig} ({contrib:+.3f})")
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
    st_contrib = st_v.get("contrib", 0.0)
    parts.append(f"ST {st_state} {vsig} ({st_contrib:+.3f})")

    # ── RSI ───────────────────────────────────────────────────────────
    try:
        rsi_val = float(_rsi(closes, settings.trade_rsi_period).iloc[-1])
        rsi_v = votes.get("rsi", {})
        vsig = _SIG.get(rsi_v.get("signal", "hold"), "─홀드")
        contrib = rsi_v.get("contrib", 0.0)
        parts.append(
            f"RSI {rsi_val:.1f} "
            f"(기준 {settings.trade_rsi_oversold:.0f}/{settings.trade_rsi_overbought:.0f}) "
            f"{vsig} ({contrib:+.3f})"
        )
    except Exception:
        pass

    # ── Bollinger ─────────────────────────────────────────────────────
    try:
        bb_mid = float(closes.rolling(settings.trade_bb_window).mean().iloc[-1])
        bb_std = float(closes.rolling(settings.trade_bb_window).std().iloc[-1])
        bb_upper = bb_mid + settings.trade_bb_k * bb_std
        bb_lower = bb_mid - settings.trade_bb_k * bb_std
        bb_v = votes.get("bollinger", {})
        vsig = _SIG.get(bb_v.get("signal", "hold"), "─홀드")
        contrib = bb_v.get("contrib", 0.0)
        parts.append(f"BB {bb_lower:,.0f}~{bb_upper:,.0f}원 {vsig} ({contrib:+.3f})")
    except Exception:
        pass

    # ── DailyContext ──────────────────────────────────────────────────
    dc_v = votes.get("daily_context", {})
    dc_reason = dc_v.get("reason", "")
    dc_sig = dc_v.get("signal", "hold")
    dc_contrib = dc_v.get("contrib", 0.0)
    if "gate1" in dc_reason:
        dc_str = "DC 당일진입(게이트1미달-보유1일미만)"
    elif "gate2" in dc_reason:
        m = re.search(r"수익[=]?([+-]?[\d.]+)%\s*<\s*([\d.]+)%", dc_reason)
        if m:
            dc_str = f"DC 수익{m.group(1)}%<{m.group(2)}%(게이트2-수익률미달)"
        else:
            m2 = re.search(r"수익[=]?([+-]?[\d.]+)%", dc_reason)
            pct = m2.group(1) if m2 else "?"
            dc_str = f"DC 수익{pct}%<{settings.daily_context_profit_gate_pct}%(게이트2-수익률미달)"
    elif "플로팅" in dc_reason or ("게이트 통과" in dc_reason):
        m = re.search(r"수익([+-]?[\d.]+)%", dc_reason)
        pct = m.group(1) if m else "?"
        cands = dc_reason.split("[")[-1].rstrip("]") if "[" in dc_reason else ""
        dc_str = f"DC 수익{pct}%(게이트통과) 플로팅미달[{cands}]"
    elif dc_sig == "sell":
        m = re.search(r"수익[=]?([+-]?[\d.]+)%", dc_reason)
        pct = m.group(1) if m else "?"
        dc_str = f"DC 장기보유청산(수익{pct}%) ▼매도"
    else:
        dc_str = "DC ─홀드"
    parts.append(f"{dc_str} ({dc_contrib:+.3f})")

    # ── 거래량 필터 ───────────────────────────────────────────────────
    vfr = meta.get("vol_filter_result", {})
    if vfr and vfr.get("action", "inactive") not in ("inactive", "off"):
        _VFR_ICON = {
            "boost": "📈", "boost_sell": "📈↓",
            "penalty": "📉", "penalty_sell": "📉↑",
            "voter_buy": "🗳️↑", "voter_sell": "🗳️↓",
            "neutral": "〰️",
        }
        action  = vfr.get("action", "neutral")
        ratio   = vfr.get("ratio", 0.0)
        applied = vfr.get("applied", 0.0)
        high_thr = vfr.get("high_thr", 1.2)
        low_thr  = vfr.get("low_thr", 0.7)
        icon = _VFR_ICON.get(action, "🔢")
        if action == "neutral":
            parts.append(f"{icon} 거래량 {ratio:.2f}x (임계 ≥{high_thr}/≤{low_thr}) 중립")
        else:
            parts.append(f"{icon} 거래량 {ratio:.2f}x (임계 ≥{high_thr}/≤{low_thr}) {action} ({applied:+.4f})")

    # ── 뉴스 ─────────────────────────────────────────────────────────
    news_bias = meta.get("news_bias", 0)
    news_n = meta.get("news_article_count", 0)
    if news_n > 0:
        parts.append(f"뉴스 {news_bias:+.3f} ({news_n}건)")

    detail = "\n    ".join(parts)
    # ATR 손절 정보 (활성 시만) — meta 에 저장된 실제 사용값 우선, 없으면 settings 값
    atr_str = ""
    if settings.atr_stop_loss_enabled or settings.position_sizing == "atr":
        _actual_stop = meta.get("effective_stop_pct", settings.trade_stop_loss_pct)
        atr_str = f" | 손절 -{_actual_stop:.2f}%(ATR)"
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
        contrib = v.get("contrib", 0.0)
        icon = "✅" if sig == "buy" else "🔴" if sig == "sell" else "⬜"
        label = _STRATEGY_KO.get(name, name.upper())
        score_str = f" ({contrib:+.3f})" if contrib != 0.0 else " (0.000)"
        lines.append(f"{icon} {label}{score_str}: {_vote_sentence(name, raw, sig)}")

    sr_adj = meta.get("sr_adj", 0.0)
    sr_tag = meta.get("sr_tag", "")
    if sr_tag:
        icon = "📍" if sr_adj > 0 else "🚧"
        lines.append(f"{icon} S/R: {sr_tag} (점수 {sr_adj:+.2f})")

    vfr = meta.get("vol_filter_result", {})
    if vfr and vfr.get("action", "inactive") not in ("inactive", "off"):
        _VFR_ICON = {
            "boost":       "📈",
            "boost_sell":  "📈↓",
            "penalty":     "📉",
            "penalty_sell":"📉↑",
            "voter_buy":   "🗳️↑",
            "voter_sell":  "🗳️↓",
            "neutral":     "〰️",
        }
        _ACTION_KO = {
            "boost": "상승 부스트", "boost_sell": "매도 강화",
            "penalty": "하락 패널티", "penalty_sell": "매도 완화",
            "voter_buy": "투표 매수", "voter_sell": "투표 매도",
        }
        action = vfr.get("action", "neutral")
        ratio = vfr.get("ratio", 0.0)
        applied = vfr.get("applied", 0.0)
        high_thr = vfr.get("high_thr", 1.2)
        low_thr = vfr.get("low_thr", 0.7)
        mode = vfr.get("mode", "")
        icon = _VFR_ICON.get(action, "🔢")
        thr_str = f"임계 ≥{high_thr}/≤{low_thr}"
        if action == "neutral":
            lines.append(f"{icon} 거래량: {ratio:.2f}x ({thr_str}) → 중립 (조정 없음)")
        else:
            lines.append(
                f"{icon} 거래량: {ratio:.2f}x [{thr_str}] "
                f"→ {_ACTION_KO.get(action, action)} (점수 {applied:+.4f}, 모드={mode})"
            )

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
    # 분봉 모드: HTF MA 오버라이드(EMA120)용 히스토리 확보 → 최소 135봉
    if settings.live_candle == "minute":
        lookback = max(lookback, 135)

    symbols_to_run = [s for s in settings.symbols if not only_symbols or s in only_symbols]
    for symbol in symbols_to_run:
        try:
            # ── 지연매도 체결: 이전 틱에서 큐된 매도를 이번 봉 시가(현재가)로 체결 ──
            if symbol in _pending_sell:
                _ps = _pending_sell.pop(symbol)
                _ps_qty = _ps.get("sell_qty", 0)
                _ps_avg = _ps.get("avg_price", 0.0)
                _ps_dec = _ps.get("decision")
                if _ps_qty > 0:
                    try:
                        _ps_price_raw = broker.get_current_price(symbol)
                        _ps_price = float(_ps_price_raw) if _ps_price_raw else 0.0
                    except Exception:
                        _ps_price = 0.0
                    if _ps_price > 0:
                        resp = broker.place_order(symbol, "sell", _ps_qty)
                        _pnl_pct = ((_ps_price - _ps_avg) / _ps_avg * 100) if _ps_avg > 0 else 0.0
                        _pnl_str = f"{'▲' if _pnl_pct >= 0 else '▼'} {_pnl_pct:+.2f}%"
                        _nm = get_name(symbol)
                        record_trade(
                            symbol, "sell", _ps_qty, _ps_price,
                            f"지연매도 체결 (이전 봉 신호 → 이번 봉 시가)",
                            json.dumps(resp, ensure_ascii=False),
                            strategy=settings.trade_strategy,
                        )
                        metrics.orders_total.labels(symbol=symbol, side="sell", mode=mode).inc()
                        notify(
                            f"🔵 **매도(지연)** {symbol}{f' ({_nm})' if _nm else ''} {_ps_qty}주 @ {_ps_price:,.0f}원\n"
                            f"수익률: {_pnl_str} (평단 {_ps_avg:,.0f}원)\n"
                            f"시간: {_now_kst()}\n\n지연매도: 이전 봉 신호 → 이번 봉 시가 체결"
                        )
                        logger.info(
                            "{} 지연매도 체결: {}주 @ {:,.0f}원 (수익 {:+.2f}%)",
                            symbol, _ps_qty, _ps_price, _pnl_pct,
                        )

            ohlcv_raw: list = []  # ATR 계산용 (어제 봉 포함, 변동성 추정 안정화)
            _closes_src: list = []  # BB/RSI 히스토리용 (오늘+어제 봉 포함)
            if settings.live_candle == "minute":
                _closes_src = broker.get_minute_ohlcv(
                    symbol, interval_min=settings.live_minute_interval, count=lookback
                )
                # stck_bsop_date는 현재 영업일을 전체 봉에 동일하게 찍으므로 날짜로 구분 불가.
                # 09:00 이후 경과 시간으로 오늘 정규장에서 생성될 수 있는 최대 봉 수를 계산해 자름.
                _now_dt = datetime.now(tz=_KST)
                _market_open = _now_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                _elapsed_min = max(0, (_now_dt - _market_open).total_seconds() / 60)
                _max_bars = int(_elapsed_min / settings.live_minute_interval) + 1
                # KIS는 최신이 앞(역순)이므로 앞에서 _max_bars개만 취함 (VWAP/ST용 오늘 봉)
                ohlcv = _closes_src[:_max_bars]
                ohlcv_raw = ohlcv  # ATR 계산용 (정규장 봉만)
            else:
                ohlcv = broker.get_daily_ohlcv(symbol, count=lookback)
                _closes_src = ohlcv
                ohlcv_raw = ohlcv
            # KIS 는 최신이 앞이므로 역순 정렬 (오래된→최신)
            ohlcv_asc = list(reversed(ohlcv))
            # BB/RSI: 전체 히스토리 closes 사용 → 장 초반부터 바로 계산 가능 (VWAP/ST는 오늘 봉만)
            closes = pd.Series([row["close"] for row in reversed(_closes_src)])

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
                        _price_eb = float(ohlcv[0].get("close", 0) or 0)
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
                            _atr_src_sl = ohlcv_raw if ohlcv_raw else ohlcv
                            _atr_val_sl = atr_from_ohlcv(list(reversed(_atr_src_sl)), period=settings.atr_period)
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
            ohlcv_df_hist: pd.DataFrame | None = None  # ST용 히스토리 (오늘+어제)
            if settings.live_candle == "minute":
                try:
                    ohlcv_df = pd.DataFrame(ohlcv_asc)[["open", "high", "low", "close", "volume"]]
                    ohlcv_df = ohlcv_df.apply(pd.to_numeric, errors="coerce")
                except Exception:
                    ohlcv_df = None
                try:
                    _hist_asc = list(reversed(_closes_src))
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
            # ATR 은 변동성 지표라 당일/어제 무관 — 원본 ohlcv(어제 포함)로 안정적 추정
            effective_stop_pct = settings.trade_stop_loss_pct
            if settings.position_sizing == "atr" or settings.atr_stop_loss_enabled:
                _atr_src = ohlcv_raw if ohlcv_raw else ohlcv
                atr_val = atr_from_ohlcv(list(reversed(_atr_src)), period=settings.atr_period)
                last_price_tmp = float(closes.iloc[-1])
                if atr_val > 0 and last_price_tmp > 0:
                    dynamic_pct = (atr_val * settings.atr_stop_multiplier) / last_price_tmp * 100
                    effective_stop_pct = min(dynamic_pct, settings.atr_stop_max_pct)

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
            # ── HTF 하락추세 매수 차단 ────────────────────────────────────────────
            # HTF_BLOCK_TF_MINUTES 봉 EMA(HTF_BLOCK_EMA_PERIOD) 하락 시 신규 매수 차단
            # 매 HTF 봉 경계에만 재계산, 그 사이 틱은 캐시 재사용
            _htf_is_down = False
            if settings.htf_block_enabled and settings.live_candle == "minute" and ohlcv_df_hist is not None:
                try:
                    _tf_min   = settings.htf_block_tf_minutes
                    _ema_per  = settings.htf_block_ema_period
                    _now_dt   = datetime.now(tz=_KST)
                    _bar_key  = _now_dt.replace(
                        minute=(_now_dt.minute // _tf_min) * _tf_min,
                        second=0, microsecond=0,
                    )
                    _cached   = _htf_trend_cache.get(symbol)
                    if _cached is None or _cached[0] != _bar_key:
                        _htf_df = ohlcv_df_hist.copy()
                        _interval = settings.live_minute_interval
                        _end_ts   = _now_dt.replace(second=0, microsecond=0)
                        _htf_df.index = pd.date_range(
                            end=_end_ts, periods=len(_htf_df),
                            freq=f"{_interval}min", tz=_KST,
                        )
                        _htf_resampled = _htf_df.resample(
                            f"{_tf_min}min", label="left", closed="left"
                        ).agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
                        ).dropna(subset=["close"])
                        if len(_htf_resampled) >= 2:
                            _htf_closed  = _htf_resampled.iloc[:-1]
                            _ema_htf     = _htf_closed["close"].ewm(span=_ema_per, adjust=False).mean()
                            _last_close  = float(_htf_closed["close"].iloc[-1])
                            _last_ema    = float(_ema_htf.iloc[-1])
                            _htf_is_down = _last_close < _last_ema
                            _htf_trend_cache[symbol] = (_bar_key, _htf_is_down)
                            logger.debug(
                                "{} [HTF] {}분봉 EMA{} 종가 {:,.0f} / EMA {:,.0f} → {}",
                                symbol, _tf_min, _ema_per, _last_close, _last_ema,
                                "하락▼" if _htf_is_down else "상승▲",
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

            # ── HTF 하락추세 시 신규 매수 완전 차단 ──────────────────────────────
            # 포지션 없을 때 BUY 신호만 차단 (매도/손절은 정상 동작)
            # 예외(MA 오버라이드): 현재가가 5분봉 EMA 근접 → 지지 반등 포착, 차단 해제
            if _htf_is_down and decision.signal is MACrossSignal.BUY and qty == 0:
                _ma_override = False
                if settings.htf_ma_override_enabled and ohlcv_df_hist is not None and len(ohlcv_df_hist) >= 20:
                    _hist_close = ohlcv_df_hist["close"]
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
                        "{} [HTF-차단] {}분봉 EMA{} 하락추세 -> 신규 매수 차단",
                        symbol, settings.htf_block_tf_minutes, settings.htf_block_ema_period,
                    )
                    from stock_bot.strategy.base import Decision
                    decision = Decision(MACrossSignal.HOLD, "htf-downtrend-block")

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
                                f"entry-block BUY 차단",
                                meta={**(decision.meta or {}), "decision": "entry_blocked"},
                            )
                        # (3) SELL 은 모두 통과 (일반/stop_loss 무관, 앙상블 결정 신뢰)
                        elif decision.signal == MACrossSignal.SELL and qty > 0:
                            _kind = (decision.meta or {}).get("kind", "")
                            logger.info(
                                "{} [entry-block] SELL 통과 (kind={}, 수익 {:+.2f}%)",
                                symbol, _kind or "ensemble", _profit_pct,
                            )
                except Exception as exc:
                    logger.warning("entry_block parse error: {}", exc)

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
                    symbol, "buy", sizing.quantity, price, reason, json.dumps(resp, ensure_ascii=False),
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
                _immediate_sell_kinds = {"stop_loss", "entry_block_force_sell", "news_critical_sell"}
                _is_immediate = _sell_kind in _immediate_sell_kinds

                if not _is_immediate:
                    # 일반 앙상블 매도 → 다음 봉 시가 지연 체결
                    _pending_sell[symbol] = {
                        "decision": decision,
                        "sell_qty": _sell_qty,
                        "avg_price": avg,
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
                        f"시간: {_now_kst()}"
                    )
                else:
                    # 손절 / 강제매도 → 즉시 체결
                    resp = broker.place_order(symbol, "sell", _sell_qty)
                    if _sell_kind == "entry_block_force_sell":
                        _mark_force_sold(symbol)
                    if _sell_kind == "stop_loss":
                        _mark_stop_loss(symbol)
                    record_trade(
                        symbol, "sell", _sell_qty, price, sell_reason, json.dumps(resp, ensure_ascii=False),
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
                    _partial_note = (
                        f" ({_sell_qty}/{qty}주 분할매도, 잔량 {qty - _sell_qty}주)"
                        if _sell_qty < qty else ""
                    )
                    _sell_label = "🟠 **분할매도**" if _sell_qty < qty else "🔵 **매도**"
                    notify(
                        f"{_sell_label} {symbol}{f' ({_nm})' if _nm else ''} {_sell_qty}주 @ {price:,.0f}원{_partial_note}\n"
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
