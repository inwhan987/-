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

SYSTEM = (
    "너는 한국 주식 자동매매 봇의 하루 거래 내역을 리뷰하는 시니어 트레이더다. "
    "주어진 거래 로그와 각 거래의 시그널/뉴스 컨텍스트를 읽고 "
    "오늘의 의사결정 품질을 평가하라. 반드시 JSON 객체 하나만 출력한다. "
    "설명, 주석, 마크다운 펜스 금지."
)

USER_TEMPLATE = """오늘({date}) 봇이 체결한 거래 목록이다. 총 {n}건.

각 항목은 ts, symbol, side, quantity, price, strategy, reason 와
details(시그널 meta, 뉴스 컨텍스트, 사이징) 를 포함한다.

```json
{trades}
```

평가 스키마(엄수):
{{
  "summary": "1~3문장 한국어 총평",
  "findings": [
    "발견한 패턴/문제/강점 한 줄 문자열 …",
    "…"
  ],
  "suggestions": [
    "내일부터 조정하면 좋을 구체적 제안 (예: buy_threshold 0.6→0.55, news_veto -0.4→-0.3)",
    "…"
  ]
}}

주의:
- findings/suggestions 는 각 3~6개, 너무 일반적인 조언 금지.
- 제안은 가능하면 파라미터명과 방향을 명시.
- 거래가 0건이면 summary 에 사유 가설(뉴스 부재/추세 없음 등) 을 적고
  findings/suggestions 는 빈 배열로 반환해도 된다."""


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
