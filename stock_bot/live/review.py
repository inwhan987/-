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

MODEL = "claude-haiku-4-5-20251001"

SYSTEM = """\
너는 한국 주식 자동매매 봇의 하루 거래 내역을 리뷰하는 시니어 퀀트 트레이더다.

## 봇 구성 (현재 버전 기준)
- 전략: 앙상블 4-전략 투표제
  · VWAP 평균회귀 (가중치 35%) — 5분봉 VWAP 대비 ±0.5% 이탈 시 신호
  · Supertrend 7/3 (가중치 30%) — ATR 기반 추세 전환 감지
  · RSI 14기간 35/65 (가중치 20%) — 과매도/과매수 기준
  · Bollinger 20/2 (가중치 15%) — 밴드 이탈 반등
- 매수 조건: 가중합 점수 ≥ 0.4 AND 2표 이상 BUY
- 매도 조건: 가중합 점수 ≤ -0.3 AND 2표 이상 SELL
- 일봉 S/R 필터: 지지 근처 +0.10, 저항 근처 -0.15, 저항 돌파+ST+거래량 +0.20
- 포지션 사이징: 계좌의 20% 비율 (fraction 모드)
- 손절: 평단 대비 -5%
- 뉴스 modulator: 감성점수 × 0.3 가산, 강한 부정(≤-0.6) 시 매수 거부
- 캔들: 5분봉 (KRX 09:00~15:30 KST)
- 수수료: 매수 0.015%, 매도 0.195% (세금 포함)

## 거래 로그 필드 설명
- weighted_score: 4전략 가중합 (-1 ~ +1). 매수 문턱 0.4 / 매도 문턱 -0.3
- buy_votes/sell_votes: BUY/SELL 찬성 전략 수 (최소 2개 필요)
- sr_adj/sr_tag: 일봉 S/R 보정값과 이유
- news_bias: 뉴스 감성이 점수에 기여한 값
- votes[].signal: 각 서브전략의 신호 (buy/sell/hold)
- reason: 한국어 거래 이유 서술 (전략별 판단 포함)

주어진 거래 로그를 읽고 오늘의 의사결정 품질을 평가하라.
반드시 JSON 객체 하나만 출력한다. 설명, 주석, 마크다운 펜스 금지.\
"""

USER_TEMPLATE = """오늘({date} KST) 봇이 체결한 거래 목록이다. 총 {n}건.

각 항목에는 ts(KST), symbol, side, quantity, price, strategy, reason,
그리고 details(meta.votes, meta.weighted_score, meta.buy_votes, meta.sell_votes,
meta.sr_adj, meta.sr_tag, news.score, news.article_count, sizing) 가 포함된다.

```json
{trades}
```

다음 관점으로 평가하라:
1. 매수/매도 타이밍: 점수와 투표 분포가 적절했나? 아슬아슬한 진입(점수 0.4~0.5)이 많았나?
2. S/R 필터 효과: sr_adj 가 결정에 영향을 줬나? 지지/저항 근처 거래 품질은?
3. 전략 일치도: 어떤 서브전략들이 자주 동의/불일치했나? VWAP·Supertrend·RSI·볼린저 중 오신호가 있었나?
4. 뉴스 영향: news_bias 가 컸던 거래가 있나? 뉴스 가중이 적절했나?
5. 보유 시간/횟수: 매수→매도까지 너무 빠르거나 느렸나? 오늘 거래 빈도는 적절한가?

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
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        logger.warning("리뷰 JSON 파싱 실패: {}", raw[:200])
        return {}
    return json.loads(m.group(0))


def run_daily_review(date: str | None = None) -> int | None:
    """지정 날짜(기본 오늘) 의 거래를 리뷰하고 ReviewLog 에 저장. 새 row id 반환."""
    date_str = date or datetime.now(tz=_KST).strftime("%Y-%m-%d")
    trades = _today_trades(date_str)
    logger.info("daily review {} — {}건", date_str, len(trades))

    if not trades:
        rid = record_review(
            date=date_str,
            trades_count=0,
            summary=f"{date_str} 체결 없음.",
            findings=[],
            suggestions=[],
            raw_context="",
        )
        notify(f"📊 **{date_str} 장마감 리뷰**\n체결 없음.")
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
