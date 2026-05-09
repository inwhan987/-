"""장마감 후 Claude 를 이용한 당일 거래 리뷰.

15:35 KST 에 호출되어:
  1. 오늘 날짜의 TradeLog 전부 로드 (strategy, details JSON 포함)
  2. Claude Haiku 에 전달해 요약/발견/제안 JSON 을 요청
  3. `ReviewLog` 에 저장

결과는 웹 `/reasons` 탭에서 열람 가능.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone

_KST = timezone(timedelta(hours=9))

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from stock_bot.notify import notify
from stock_bot.storage import ENGINE, TradeLog, record_review
from stock_bot.config import settings as _settings

MODEL = "claude-sonnet-4-6"

_SYSTEM_BASE = """\
너는 한국 주식 단기 자동매매 시스템을 운용·개선해온 퀀트 트레이더다.

## 전문성
- 코스피/코스닥 단기 모멘텀·평균회귀 전략 10년 이상 운용 경험
- 앙상블 전략 설계 및 파라미터 튜닝 전문 (과최적화 위험 항상 인지)
- 분봉 VWAP·Supertrend·RSI·볼린저밴드 신호 해석에 정통
- 뉴스 감성 가중치가 단기 매매에 미치는 영향 숙지

## 리뷰 원칙
- 칭찬·격려·일반론 금지. 핵심 문제와 근거만 서술
- 제안은 반드시 파라미터명·현재값·변경 방향·기대 효과를 명시
- 과거 데이터 1일치만으로 단정적 결론 내리지 않음 (가설로 서술)
- 시장 맥락(당일 장세·변동성)을 먼저 파악하고 봇 행동을 해석

{strategy_context}

## 거래 로그 필드 설명
- weighted_score: 4전략 가중합 (-1 ~ +1)
- buy_votes/sell_votes: BUY/SELL 찬성 전략 수
- news_bias: 뉴스 감성이 점수에 기여한 값
- votes[].signal: 각 서브전략의 신호 (buy/sell/hold)
- reason: 한국어 거래 이유 서술 (전략별 판단 포함)

주어진 거래 로그를 읽고 오늘의 의사결정 품질을 평가하라.
반드시 JSON 객체 하나만 출력한다. 설명, 주석, 마크다운 펜스 금지.\
"""


def _build_strategy_context() -> str:
    """현재 settings 값을 읽어 전략 구성 문자열 동적 생성.

    .env.overrides 변경 → 핫리로드 → 다음 리뷰에 자동 반영.
    """
    from stock_bot.config import settings

    try:
        w = settings.ensemble_weights_tuple
    except Exception:
        w = (0.35, 0.30, 0.20, 0.15)

    sizing_desc = {
        "fraction": f"계좌의 {settings.position_fraction * 100:.0f}% 비율",
        "atr":      f"ATR 기반 리스크 {settings.risk_per_trade_pct:.1f}% (최대 {settings.max_position_pct:.0f}%)",
        "fixed":    f"고정 {settings.trade_cash_per_trade:,}원",
    }.get(settings.position_sizing, settings.position_sizing)

    news_line = (
        f"어드바이저리 — 감성점수 × {settings.ensemble_news_weight} 가산 (하드 veto 없음, "
        f"나쁜 뉴스는 weighted_score를 낮춰 매수 임계값 통과를 어렵게 만드는 방식)"
        if settings.ensemble_use_news and settings.news_enabled
        else "비활성"
    )

    dc_weight = w[4] * 100 if len(w) >= 5 else 0
    return (
        f"## 봇 구성 (실시간 settings 기준)\n"
        f"- 전략: 앙상블 5-전략 투표제\n"
        f"  · VWAP 평균회귀       (가중치 {w[0]*100:.0f}%) — ±{settings.trade_vwap_band*100:.1f}% 이탈, "
        f"개장 후 {settings.trade_vwap_warmup_bars}봉({settings.trade_vwap_warmup_bars * settings.live_minute_interval}분) 워밍업\n"
        f"  · Supertrend {settings.trade_supertrend_period}/{settings.trade_supertrend_mult} "
        f"(가중치 {w[1]*100:.0f}%) — ATR 추세 전환\n"
        f"  · RSI {settings.trade_rsi_period}기간 "
        f"{settings.trade_rsi_oversold:.0f}/{settings.trade_rsi_overbought:.0f} "
        f"(가중치 {w[2]*100:.0f}%) — 과매도/과매수\n"
        f"  · Bollinger {settings.trade_bb_window}/{settings.trade_bb_k} "
        f"(가중치 {w[3]*100:.0f}%) — 밴드 이탈 반등\n"
        f"  · DailyContext        (가중치 {dc_weight:.0f}%) — 1일 이상 보유 포지션 수익 "
        f"≥{settings.daily_context_profit_gate_pct:.1f}% 시 청산 (SELL/HOLD 전용)\n"
        f"- 매수: 점수 ≥ {settings.ensemble_buy_threshold} AND {settings.ensemble_min_buy_votes}표 이상\n"
        f"- 매도: 점수 ≤ {settings.ensemble_sell_threshold} AND {settings.ensemble_min_sell_votes}표 이상\n"
        f"- 포지션: {sizing_desc}\n"
        f"- 손절: {'ATR 동적(' + str(settings.atr_stop_multiplier) + 'x ATR' + str(settings.atr_period) + ')' if settings.atr_stop_loss_enabled else f'고정 -{settings.trade_stop_loss_pct:.1f}%'}\n"
        f"- 뉴스: {news_line}\n"
        f"- 캔들: {settings.live_minute_interval}분봉 / 수수료 매수 0.015% 매도 0.195%"
    )


def _build_system() -> str:
    return _SYSTEM_BASE.format(strategy_context=_build_strategy_context())

USER_TEMPLATE = """오늘({date} KST) 봇이 체결한 거래 목록이다. 총 {n}건.

각 항목에는 ts(KST), symbol, side, quantity, price, strategy, reason,
그리고 details(meta.votes, meta.weighted_score, meta.buy_votes, meta.sell_votes,
news.score, news.article_count, sizing) 가 포함된다.

```json
{trades}
```

다음 관점으로 평가하라:
1. 매수/매도 타이밍: 점수와 투표 분포가 적절했나? 매수 임계 근방(buy_threshold ± 0.05) 진입이 많았나?
2. 전략 일치도: 어떤 서브전략들이 자주 동의/불일치했나? VWAP·Supertrend·RSI·볼린저·DailyContext 중 오신호가 있었나? (VWAP는 개장 후 60분 워밍업 있음)
3. 뉴스 어드바이저리 품질: news_bias와 실제 주가 방향이 일치했나?
   - news_bias가 양수였는데 주가가 떨어졌다면 → 뉴스 감성 점수가 과도하게 낙관적이었음 (LLM 프롬프트 조정 또는 news_weight 축소 제안)
   - news_bias가 음수였는데 주가가 올랐다면 → 뉴스 감성 점수가 과도하게 비관적이었음 (동일)
   - news_bias가 방향 판단에 도움이 됐다면 → 현행 유지 근거 명시
4. 보유 시간/횟수: 매수→매도까지 너무 빠르거나 느렸나? 오늘 거래 빈도는 적절한가?

평가 스키마(엄수):
{{
  "summary": "1~3문장 한국어 총평 (오늘 장 특성, 봇 행동, 결과 포함)",
  "findings": [
    "구체적 패턴/문제/강점. 예: VWAP가 3번 중 2번 오신호 (횡보장 과잉반응)",
    "…"
  ],
  "suggestions": [
    "구체적 파라미터 조정 제안. 예: vwap_band 0.005→0.007 (오신호 빈도 감소 목적)",
    "…"
  ]
}}

주의:
- findings/suggestions 각 3~6개. 너무 일반적인 조언 금지.
- 제안은 반드시 파라미터명과 변경 방향/수치를 명시할 것.
- 거래가 0건이면: summary에 시장 상황 가설 서술, findings/suggestions 빈 배열 반환."""


def _today_trades(date_str: str) -> list[dict]:
    start = datetime.strptime(date_str, "%Y-%m-%d")
    end = start + timedelta(days=1)
    with Session(ENGINE) as s:
        rows = s.scalars(
            select(TradeLog).where(TradeLog.ts >= start).where(TradeLog.ts < end).order_by(TradeLog.ts)
        ).all()
        out: list[dict] = []
        for r in rows:
            try:
                details = json.loads(r.details) if r.details else {}
            except Exception:
                details = {}
            kst_ts = r.ts.replace(tzinfo=timezone.utc).astimezone(_KST)
            out.append(
                {
                    "ts": kst_ts.strftime("%H:%M:%S KST"),
                    "symbol": r.symbol,
                    "side": r.side,
                    "quantity": r.quantity,
                    "price": r.price,
                    "strategy": r.strategy,
                    "reason": r.reason,
                    "details": details,
                }
            )
        return out


NO_TRADE_TEMPLATE = """오늘({date} KST) 봇이 체결한 거래가 0건이다.

## 봇 앙상블 임계값
- 매수: weighted_score ≥ {buy_thr} AND {min_buy_votes}표 이상
- 매도: weighted_score ≤ {sell_thr} AND {min_sell_votes}표 이상
- 손절: -{stop_pct:.1f}%
- 뉴스: 어드바이저리 (하드 veto 없음 — 감성점수가 weighted_score에 가산되는 방식)

## 종목 목록
{symbols}

다음 관점으로 분석하라:
1. 오늘 거래가 없었던 가능한 시장 상황 (횡보·저변동성·임계값 미달 등)
2. 앙상블 전략 특성상 체결이 안 나오기 쉬운 조건
3. 현재 파라미터 임계값이 오늘 같은 날에 적절했는지

평가 스키마(엄수):
{{
  "summary": "1~3문장 — 오늘 무체결 원인 가설과 봇 상태 설명",
  "findings": ["구체적 관찰 3~5개"],
  "suggestions": ["파라미터 조정 제안 또는 현행 유지 근거 2~4개"]
}}
JSON만 출력. 설명·마크다운 금지."""


def _call_claude_no_trade(date_str: str) -> dict:
    """체결 없는 날 — 앙상블 미반응 원인 분석."""
    try:
        from anthropic import Anthropic
    except ImportError:
        return {}
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {}
    client = Anthropic(api_key=key)
    sym_list = ", ".join(
        f"{s}" for s in _settings.symbols
    )
    prompt = NO_TRADE_TEMPLATE.format(
        date=date_str,
        buy_thr=_settings.ensemble_buy_threshold,
        sell_thr=_settings.ensemble_sell_threshold,
        min_buy_votes=_settings.ensemble_min_buy_votes,
        min_sell_votes=_settings.ensemble_min_sell_votes,
        stop_pct=_settings.trade_stop_loss_pct,
        symbols=sym_list,
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=_build_system(),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    try:
        from stock_bot.costs import record_cost
        record_cost("daily_review", resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception:
        pass
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        logger.warning("무체결 리뷰 JSON 파싱 실패: {}", raw[:300])
        return {}
    try:
        return json.loads(m.group(0))
    except Exception as e:
        logger.warning("무체결 리뷰 JSON 디코딩 실패: {} | raw={}", e, raw[:300])
        return {}


def _call_claude(date_str: str, trades: list[dict]) -> dict:
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("anthropic 미설치 — 리뷰 건너뜀")
        return {}
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("ANTHROPIC_API_KEY 없음 — 리뷰 건너뜀")
        return {}
    client = Anthropic(api_key=key)
    prompt = USER_TEMPLATE.format(
        date=date_str, n=len(trades), trades=json.dumps(trades, ensure_ascii=False, indent=2)
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_build_system(),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    try:
        from stock_bot.costs import record_cost
        record_cost("daily_review", resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception:
        pass
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        logger.warning("리뷰 JSON 파싱 실패: {}", raw[:200])
        return {}
    return json.loads(m.group(0))


def run_daily_review(date: str | None = None) -> int | None:
    """지정 날짜(기본 오늘) 의 거래를 리뷰하고 ReviewLog 에 저장. 새 row id 반환."""
    now_kst = datetime.now(tz=_KST)
    date_str = date or now_kst.strftime("%Y-%m-%d")

    # 공휴일 체크: exchange_calendars로 거래일이 아니면 스킵
    if date is None:
        try:
            import exchange_calendars as xcals
            cal = xcals.get_calendar("XKRX")
            if not cal.is_session(date_str):
                logger.info("daily review skip — {} 는 KRX 휴장일", date_str)
                return None
        except Exception:
            pass  # 라이브러리 없으면 그냥 진행

    trades = _today_trades(date_str)
    logger.info("daily review {} — {}건", date_str, len(trades))

    if not trades:
        try:
            result = _call_claude_no_trade(date_str)
        except Exception as exc:
            logger.exception("무체결 리뷰 Claude 호출 실패: {}", exc)
            result = {}
        summary = str(result.get("summary", "")) or f"{date_str} 체결 없음."
        findings = result.get("findings", []) if isinstance(result.get("findings"), list) else []
        suggestions = result.get("suggestions", []) if isinstance(result.get("suggestions"), list) else []
        rid = record_review(
            date=date_str,
            trades_count=0,
            summary=summary,
            findings=findings,
            suggestions=suggestions,
            raw_context="",
        )
        lines = [f"📊 **{date_str} 장마감 리뷰** (체결 0건)", "", summary]
        if findings:
            lines.append("\n**분석**")
            for f in findings[:5]:
                lines.append(f"• {f}")
        if suggestions:
            lines.append("\n**제안**")
            for s in suggestions[:4]:
                lines.append(f"• {s}")
        notify("\n".join(lines))
        return rid

    try:
        result = _call_claude(date_str, trades)
    except Exception as exc:
        logger.exception("Claude 리뷰 호출 실패: {}", exc)
        result = {}

    summary = str(result.get("summary", "")) or f"{len(trades)}건 체결 — 리뷰 생성 실패"
    findings = result.get("findings", []) if isinstance(result.get("findings"), list) else []
    suggestions = (
        result.get("suggestions", []) if isinstance(result.get("suggestions"), list) else []
    )
    raw_context = json.dumps(trades, ensure_ascii=False)
    rid = record_review(
        date=date_str,
        trades_count=len(trades),
        summary=summary,
        findings=findings,
        suggestions=suggestions,
        raw_context=raw_context,
    )
    logger.info("리뷰 저장 id={} findings={} suggestions={}", rid, len(findings), len(suggestions))

    # 디스코드 알림 (URL 없으면 자동으로 no-op)
    lines = [f"📊 **{date_str} 장마감 리뷰 (KST)** ({len(trades)}건 체결)", "", summary]
    if findings:
        lines.append("\n**발견 사항**")
        for f in findings[:6]:
            text = f if isinstance(f, str) else str(f.get("text") or f)
            lines.append(f"• {text}")
    if suggestions:
        lines.append("\n**제안 조정**")
        for s in suggestions[:6]:
            text = s if isinstance(s, str) else str(s.get("text") or s)
            lines.append(f"• {text}")
    notify("\n".join(lines))
    return rid


def _is_close_time(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now.weekday() < 5 and now.time() >= dtime(15, 35)


if __name__ == "__main__":
    import sys

    d = sys.argv[1] if len(sys.argv) > 1 else None
    run_daily_review(d)
