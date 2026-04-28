"""실시간 거래 러너.

장중 1분마다 실행하지는 않고, 기본 15분 주기로 일봉 데이터를 당겨 시그널을 계산한다.
KRX 정규장 (09:00 ~ 15:30 KST) 에만 동작.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone

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
from stock_bot.live.review import run_daily_review
from stock_bot.names import get_name
from stock_bot.news import (
    fetch_naver_news,
    init_news_db,
    news_exists,
    recent_news_articles,
    recent_sentiment_dynamic,
    save_news,
    score_sentiment,
)
from stock_bot.notify import metrics, notify
from stock_bot.sizing import SizingResult, atr_sizing, fixed_amount, fixed_fraction
from stock_bot.costs import init_costs_db
from stock_bot.storage import init_db, record_trade
from stock_bot.strategy import MACrossSignal, decide_from_settings

# .env / .env.overrides 변경 감시용
_ENV_PATH = None
_ENV_MTIME = 0.0
_OVERRIDE_MTIME = 0.0



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
    ("TRADE_CASH_PER_TRADE", "trade_cash_per_trade", int),
    ("LIVE_INTERVAL_MINUTES", "live_interval_minutes", int),
    ("LIVE_CANDLE", "live_candle", str),
    ("NEWS_ENABLED", "news_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
    ("NEWS_LOOKBACK_HOURS", "news_lookback_hours", int),
    ("ENSEMBLE_NEWS_VETO_THRESHOLD", "ensemble_news_veto_threshold", float),
    ("ENSEMBLE_NEWS_WEIGHT", "ensemble_news_weight", float),
    ("NEWS_PREFER_LLM", "news_prefer_llm", lambda v: v.lower() in ("1", "true", "yes", "on")),
)


def _reload_env_if_changed() -> None:
    """`.env` / `.env.overrides` 변경 감지 → 핫리로드.

    도커에서 env vars 가 os.environ 에 고정되므로 pydantic Settings 재인스턴스화로는
    갱신되지 않는다. 파일을 직접 파싱해 `settings` 객체 속성을 덮어쓴다.
    우선순위: .env.overrides > .env
    """
    from pathlib import Path
    global _ENV_PATH, _ENV_MTIME, _OVERRIDE_MTIME
    if _ENV_PATH is None:
        root = Path(__file__).resolve().parents[2]
        _ENV_PATH = root / ".env"
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
    if changed:
        logger.info(".env 변경 감지, 핫리로드: {}", "; ".join(changed))


_STRATEGY_KO = {
    "vwap": "VWAP",
    "supertrend": "Supertrend",
    "rsi": "RSI",
    "bollinger": "볼린저",
    "ema": "EMA크로스",
    "macd": "MACD",
    "momentum": "모멘텀",
}


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
        if "upper revert" in reason:
            return "볼린저 상단 돌파 후 회귀 → 과매수 청산 신호"
        return "볼린저 밴드 내 움직임, 신호 없음"
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




def _is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
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

    장중(09:00~15:30 KST) 에는 1분마다 실행되며,
    critical 기사가 포착되면 해당 종목에 대해 즉시 거래 tick 을 발화한다.
    """
    _reload_env_if_changed()
    if not settings.news_enabled:
        return
    trigger_symbols: set[str] = set()
    for symbol in settings.symbols:
        try:
            items = fetch_naver_news(symbol, pages=settings.news_pages_per_symbol)
            new_count = 0
            crit_count = 0
            for item in items:
                # 중복 기사는 LLM 호출 없이 건너뜀 (비용 절감)
                if news_exists(item.symbol, item.url):
                    continue
                text = f"{item.title} {item.summary}"
                result = score_sentiment(
                    text, prefer_llm=settings.news_prefer_llm, symbol=symbol
                )
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
            if new_count:
                logger.info(
                    "news {} new={} critical={} (total={})",
                    symbol, new_count, crit_count, len(items),
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

            news_score, news_count, news_critical = (0.0, 0, 0)
            if settings.news_enabled:
                news_score, news_count, news_critical = recent_sentiment_dynamic(symbol)

            # ATR 모드면 손절 거리를 동적으로 계산해 전략에 주입
            effective_stop_pct = settings.trade_stop_loss_pct
            if settings.position_sizing == "atr":
                atr_val = atr_from_ohlcv(list(reversed(ohlcv)), period=settings.atr_period)
                last_price_tmp = float(closes.iloc[-1])
                if atr_val > 0 and last_price_tmp > 0:
                    dynamic_pct = (atr_val * settings.atr_stop_multiplier) / last_price_tmp * 100
                    effective_stop_pct = dynamic_pct
            # 설정을 통해 전략에 흘려보내기
            _orig_stop = settings.trade_stop_loss_pct
            settings.trade_stop_loss_pct = effective_stop_pct
            try:
                decision = decide_from_settings(
                    closes,
                    position_qty=qty,
                    avg_price=avg,
                    news_sentiment=news_score if news_count > 0 else None,
                    news_article_count=news_count,
                    news_critical_count=news_critical,
                    ohlcv_df=ohlcv_df,
                )
            finally:
                settings.trade_stop_loss_pct = _orig_stop
            logger.info(
                "{} [{}]: {} ({})",
                symbol,
                settings.trade_strategy,
                decision.signal.value,
                decision.reason,
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
                }
                record_trade(
                    symbol, "buy", sizing.quantity, price, reason, str(resp),
                    strategy=settings.trade_strategy, details=trade_context,
                )
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
                notify(
                    f"🔴 **매수** {symbol}{f' ({_nm})' if _nm else ''} {sizing.quantity}주 @ {price:,.0f}원\n"
                    f"사이징: {sizing.method} ({sizing.note})\n"
                    f"시간: {_now_kst()}\n\n"
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
    scheduler.add_job(
        _tick,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute=f"*/{interval}",
        ),
        args=[broker],
        id="trade_tick",
    )
    if settings.news_enabled:
        # 장중: 5분마다 크롤 + critical 즉시 tick
        scheduler.add_job(
            _news_tick,
            CronTrigger(
                day_of_week="mon-fri",
                hour="9-15",
                minute="*/5",
            ),
            args=[broker],
            id="news_tick_intraday",
            next_run_time=datetime.now(),
            max_instances=1,
            coalesce=True,
        )
        # 장외: 저빈도(기본 30분) 로 유지해 오버나이트 뉴스도 수집
        scheduler.add_job(
            _news_tick,
            CronTrigger(minute=f"*/{settings.news_crawl_interval_minutes}"),
            args=[None],  # 장외에서는 tick 트리거 안 함
            id="news_tick_offhours",
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
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutting down")
    finally:
        broker.close()


if __name__ == "__main__":
    run_live()
