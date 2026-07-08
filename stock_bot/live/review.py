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
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from stock_bot.notify import notify
from stock_bot.storage import ENGINE, ReviewLog, TradeLog, record_review
from stock_bot.config import settings as _settings
from stock_bot.market_calendar import KST as _KST

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
- **파라미터는 전역(global)이다** — 특정 종목이 아니라 모든 종목에 공통 적용되는 손잡이를
  튜닝하는 것이다. 따라서 판단 근거는 **여러 영업일·여러 종목에 걸쳐 반복되는 패턴**에 둔다.
  · 서로 다른 종목에서 되풀이되는 오작동 → 전역 파라미터 문제로 보고 제안 (근거 충분).
  · 한 종목·하루 단발 효과, 또는 **현재 미보유(로테이션 아웃)된 종목의 과거 패턴** → 가설로만
    서술하고 파라미터 변경 제안 근거로 쓰지 않는다. (아래 '최근 맥락'의 현재 종목 목록 참조)
- **종목 이질성 주의**: 종목마다 분봉 변동폭(ATR%)이 2~4배 다르다. 서로 다른 종목의 raw 수치
  (밴드 이탈 횟수·손절폭 등)를 그대로 합산·평균하지 말고 각 종목 봉폭 대비로 정규화해 해석하라.
- 시장 맥락(당일 장세·지수·종목 등락률·변동성)을 먼저 파악하고 봇 행동을 해석
- **'매매 안 함'도 하나의 의사결정으로 평가하라.** 지수가 약하거나 감시종목들이 하락한 날
  봇이 진입을 안 한 것은 손실 회피 = 올바른 규율일 수 있다. 반대로 종목들이 크게 올랐는데
  못 들어갔다면 임계값이 너무 보수적이었다는 신호다. 종목 등락률을 근거로 비진입의 質을 판정하라.
  (단 무근거 칭찬은 금지 — 반드시 그날 등락률 데이터로 뒷받침)

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


def _stop_policy(s) -> tuple[str, bool]:
    """(손절 설명 문자열, ATR튜닝제안_금지여부) 반환.

    ATR 동적손절이 켜져 있어도 배수(atr_stop_multiplier)가 커서 `배수×ATR` 이
    상한 캡(atr_stop_max_pct)을 상시 초과하면 실효적으로 '고정 손절'이다.
    이 경우 리뷰가 atr_stop_multiplier 조정을 제안하는 건 무의미(캡이 지배)하고
    이미 백테스트로 기각된 방향이라, LLM 에 '실효 고정'임을 명시하고 ATR 제안을 막는다.
    배수가 캡을 상시 넘길 만큼 큰지 판단하는 임계값 6.0 은 휴리스틱
    (KR 분봉 ATR% 특성상 6x 면 대개 5% 캡에 걸림). 낮추면(<6) 진짜 동적으로 간주.
    """
    if not s.atr_stop_loss_enabled:
        return (f"고정 -{s.trade_stop_loss_pct:.1f}%", True)
    eff_fixed = s.atr_stop_multiplier >= 6.0  # 배수 큼 → 캡 상시 발동 → 실효 고정
    if eff_fixed:
        return (
            f"실효 고정 -{s.atr_stop_max_pct:.1f}% "
            f"(ATR {s.atr_stop_multiplier}x ATR{s.atr_period} 설정이나 배수가 커서 거의 항상 "
            f"상한 캡에 걸림 → ATR 동적손절 사실상 비활성. 백테스트로 검증된 의도된 구성)",
            True,
        )
    return (
        f"ATR 동적 {s.atr_stop_multiplier}x ATR{s.atr_period} (상한 캡 -{s.atr_stop_max_pct:.1f}%)",
        False,
    )


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
        f"개장 후 {settings.trade_vwap_warmup_bars}봉({settings.trade_vwap_warmup_bars * settings.live_candle_minutes}분) 워밍업\n"
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
        f"- 손절: {_stop_policy(settings)[0]}\n"
        f"- 거래량 필터: {'ON (' + str(settings.ensemble_volume_high_ratio) + '/' + str(settings.ensemble_volume_low_ratio) + ', ±' + str(settings.ensemble_volume_score_boost) + '/' + str(settings.ensemble_volume_score_penalty) + ')' if settings.ensemble_volume_filter_enabled else 'OFF'}\n"
        f"- 뉴스: {news_line}\n"
        f"- 캔들: {settings.live_candle_minutes}분봉 / 수수료 매수 0.015% 매도 0.195%"
    )


def _build_system() -> str:
    return _SYSTEM_BASE.format(strategy_context=_build_strategy_context())

USER_TEMPLATE = """오늘({date} KST) 봇이 체결한 거래 목록이다. 총 {n}건.

{market}

{recent_context}

각 항목에는 ts(KST), symbol, side, quantity, price, strategy, reason,
그리고 details(meta.votes, meta.weighted_score, meta.buy_votes, meta.sell_votes,
news.score, news.article_count, sizing) 가 포함된다.

```json
{trades}
```

다음 관점으로 평가하라:
1. 매수/매도 타이밍: 점수와 투표 분포가 적절했나? 매수 임계 근방(buy_threshold ± 0.05) 진입이 많았나?
2. 전략 일치도: 어떤 서브전략들이 자주 동의/불일치했나? VWAP·Supertrend·RSI·볼린저·DailyContext 중 오신호가 있었나? (VWAP는 개장 후 {vwap_warmup_min}분 워밍업)
3. 뉴스 어드바이저리 품질: news_bias와 실제 주가 방향이 일치했나?
   - news_bias가 양수였는데 주가가 떨어졌다면 → 뉴스 감성 점수가 과도하게 낙관적이었음 (LLM 프롬프트 조정 또는 news_weight 축소 제안)
   - news_bias가 음수였는데 주가가 올랐다면 → 뉴스 감성 점수가 과도하게 비관적이었음 (동일)
   - news_bias가 방향 판단에 도움이 됐다면 → 현행 유지 근거 명시
4. 보유 시간/횟수: 매수→매도까지 너무 빠르거나 느렸나? 오늘 거래 빈도는 적절한가?
5. 거래량 필터: reason의 vol+/vol- 태그가 신호 정확도에 기여했나? boost/penalty 값 조정 필요한가?
{stop_eval}

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
            # 대장주 선별봇 거래는 스톡봇(앙상블) 리뷰 대상이 아니므로 제외.
            # 전략·파라미터 체계가 달라 같이 평가하면 리뷰 품질이 흐려진다.
            if r.strategy == "leader_pullback":
                continue
            try:
                broker_resp = json.loads(r.broker_response) if r.broker_response else {}
            except Exception:
                broker_resp = {}
            if broker_resp.get("dry_run"):
                continue
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


def _recent_context(date_str: str, n: int = 5) -> str:
    """직전 N영업일(리뷰된 날)의 요약·제안·체결수를 되먹임용 블록으로 구성.

    전역 파라미터를 여러 날·여러 종목에 걸쳐 판단하도록 과거 리뷰를 제공한다.
    현재 SYMBOLS 목록도 함께 실어, 로테이션 아웃된 종목의 과거 패턴을 LLM이
    구분(할인)할 수 있게 한다. 과거 리뷰가 없으면 그 사실만 알린다.
    """
    from stock_bot.names import get_name

    # 현재 실제 보유 대상(로테이션 후 최신) — 과거 종목과 구분용
    cur_syms = []
    for s in _settings.symbols:
        nm = get_name(s) or ""
        cur_syms.append(f"{nm}({s})" if nm else str(s))
    cur_line = ", ".join(cur_syms) if cur_syms else "(없음)"

    with Session(ENGINE) as s:
        rows = s.scalars(
            select(ReviewLog)
            .where(ReviewLog.date < date_str)
            .order_by(ReviewLog.date.desc())
            .limit(n)
        ).all()

    lines = ["## 최근 맥락 (전역 파라미터 판단용)",
             f"- 현재 대상 종목(로테이션 최신): {cur_line}"]
    if not rows:
        lines.append(f"- 직전 리뷰 없음 — 최근 {n}영업일 누적 데이터 부재. 오늘 단독으로만 판단하라.")
        return "\n".join(lines)

    lines.append(f"- 직전 {len(rows)}영업일 리뷰 (오래된→최신, 반복 패턴만 근거로 채택):")
    for r in reversed(rows):  # 오래된 것부터
        try:
            sugg = json.loads(r.suggestions) if r.suggestions else []
        except Exception:
            sugg = []
        sugg_txt = " / ".join(
            (x if isinstance(x, str) else str(x.get("text") or x)) for x in sugg[:3]
        ) if sugg else "제안 없음"
        summ = (r.summary or "").replace("\n", " ").strip()
        if len(summ) > 140:
            summ = summ[:140] + "…"
        lines.append(f"  · {r.date} (체결 {r.trades_count}건): {summ}")
        lines.append(f"      ↳ 당시 제안: {sugg_txt}")
    lines.append(
        "  ※ 위 제안이 여러 날 반복되면 전역 파라미터 문제의 근거로 삼되, 한 종목·단발이거나 "
        "현재 대상에 없는 종목 얘기면 할인하라."
    )
    return "\n".join(lines)


def _market_snapshot() -> str:
    """당일 장세 스냅샷 — 지수 등락 + 감시종목별 등락률 + 시장폭(breadth).

    KIS get_quote(prdy_ctrt)·get_index_quote 를 best-effort 로 조회한다.
    실패해도 리뷰가 죽지 않도록 모든 단계 try/except.
    """
    from stock_bot.names import get_name

    lines: list[str] = []
    try:
        from stock_bot.broker import KISBroker
        broker = KISBroker()
    except Exception as exc:  # 브로커 생성 실패 → 스냅샷 생략
        logger.warning("market_snapshot: 브로커 생성 실패 {}", exc)
        return ""

    # 지수 (코스피/코스닥)
    idx_lines = []
    for code, nm in (("0001", "코스피"), ("1001", "코스닥")):
        try:
            q = broker.get_index_quote(code)
            if q.get("price"):
                idx_lines.append(f"{nm} {q['price']:,.2f} ({q['change_pct']:+.2f}%)")
        except Exception:
            pass
    if idx_lines:
        lines.append("## 당일 지수")
        lines.append("- " + " | ".join(idx_lines))

    # 감시종목 등락률
    rows = []
    for sym in _settings.symbols:
        try:
            q = broker.get_quote(sym)
            rows.append((sym, get_name(sym) or sym, q.change_pct))
        except Exception:
            continue
    if rows:
        ups = sum(1 for _, _, c in rows if c > 0)
        downs = sum(1 for _, _, c in rows if c < 0)
        avg = sum(c for _, _, c in rows) / len(rows)
        lines.append("")
        lines.append("## 감시종목 당일 등락률 (종가 기준 전일대비)")
        lines.append(
            f"- 시장폭: {len(rows)}종목 중 상승 {ups} / 하락 {downs} / 평균 {avg:+.2f}%"
        )
        for sym, nm, c in sorted(rows, key=lambda r: -r[2]):
            lines.append(f"  · {nm}({sym})  {c:+.2f}%")

    return "\n".join(lines)


NO_TRADE_TEMPLATE = """오늘({date} KST) 봇이 체결한 거래가 0건이다.

{market}

{recent_context}

## 봇 앙상블 임계값
- 매수: weighted_score ≥ {buy_thr} AND {min_buy_votes}표 이상
- 매도: weighted_score ≤ {sell_thr} AND {min_sell_votes}표 이상
- 손절: -{stop_pct:.1f}%
- 뉴스: 어드바이저리 (하드 veto 없음 — 감성점수가 weighted_score에 가산되는 방식)

## 종목 목록
{symbols}

다음 관점으로 분석하라:
1. 오늘 거래가 없었던 가능한 시장 상황 — 위 지수/종목 등락률을 근거로 판단 (횡보·하락·저변동성·임계값 미달 등)
2. **비진입의 質 판정 (핵심)**: 위 '감시종목 등락률'을 보고, 봇이 안 들어간 것이 옳았는지 평가하라.
   - 종목 대부분이 하락/약세였다면 → 진입 회피 = 손실 회피 = 올바른 규율 (근거: 어느 종목이 몇 % 빠졌는지 명시)
   - 종목 대부분이 크게 상승했는데 못 들어갔다면 → 임계값(buy_threshold/min_buy_votes)이 과보수적이라는 신호 (어느 종목을 놓쳤는지 명시)
3. 앙상블 전략 특성상 체결이 안 나오기 쉬운 조건
4. 현재 파라미터 임계값이 오늘 같은 날에 적절했는지

평가 스키마(엄수):
{{
  "summary": "1~3문장 — 오늘 지수/장세 + 무체결 원인 + 비진입이 옳았는지 판정 포함",
  "findings": ["구체적 관찰 3~5개 (종목 등락률 수치 인용)"],
  "suggestions": ["파라미터 조정 제안 또는 현행 유지 근거 2~4개"]
}}
JSON만 출력. 설명·마크다운 금지."""


def _salvage_review_json(raw: str) -> dict:
    """잘리거나 깨진 리뷰 JSON에서 summary·findings·suggestions만 정규식으로 최대한 복원.

    응답 본문이 max_tokens 등으로 잘려 닫는 괄호가 없어도, 앞쪽에 온전히 담긴
    summary(와 가능하면 findings 일부)를 건져 디스코드에 최소한의 리뷰를 보낸다.
    """
    out: dict = {}
    ms = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, flags=re.DOTALL)
    if ms:
        try:
            out["summary"] = json.loads(f'"{ms.group(1)}"')
        except Exception:
            out["summary"] = ms.group(1)
    for field in ("findings", "suggestions"):
        marr = re.search(rf'"{field}"\s*:\s*\[(.*?)(?:\]|$)', raw, flags=re.DOTALL)
        if not marr:
            continue
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', marr.group(1))
        vals = []
        for it in items:
            try:
                vals.append(json.loads(f'"{it}"'))
            except Exception:
                vals.append(it)
        if vals:
            out[field] = vals
    return out


def _llm_raw(prompt: str, system: str, source: str = "daily_review") -> str | None:
    """리뷰 프롬프트를 LLM 에 보내고 응답 원문 텍스트를 받는다.

    LLM_BACKEND=claude_code → Claude Code CLI(구독, 사용료 0),
    그 외 → 기존 anthropic API(롤백용). 어느 쪽이든 실패 시 None 을 반환해
    호출부가 빈 리뷰로 자연히 폴백하도록 한다(fail-safe).
    """
    from stock_bot import llm_cli
    if llm_cli.use_cli():
        from stock_bot.config.settings import settings as _s
        return llm_cli.call_cli(prompt, system=system, model=_s.daily_review_model, timeout=180)
    # ── API 경로 (LLM_BACKEND != claude_code, 롤백/기본) ──
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("anthropic 미설치 — 리뷰 건너뜀")
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("ANTHROPIC_API_KEY 없음 — 리뷰 건너뜀")
        return None
    client = Anthropic(api_key=key)
    from stock_bot.config.settings import settings as _s
    resp = client.messages.create(
        model=llm_cli.api_model_id(_s.daily_review_model, MODEL),
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        from stock_bot.costs import record_cost
        record_cost(source, resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception:
        pass
    return resp.content[0].text.strip()


def _call_claude_no_trade(date_str: str) -> dict:
    """체결 없는 날 — 앙상블 미반응 원인 분석."""
    sym_list = ", ".join(
        f"{s}" for s in _settings.symbols
    )
    try:
        market = _market_snapshot()
    except Exception as exc:
        logger.warning("market_snapshot 실패: {}", exc)
        market = ""
    try:
        recent_context = _recent_context(date_str)
    except Exception as exc:
        logger.warning("_recent_context 실패: {}", exc)
        recent_context = ""
    prompt = NO_TRADE_TEMPLATE.format(
        date=date_str,
        market=market or "(장세 데이터 조회 실패 — 등락률 정보 없음)",
        recent_context=recent_context,
        buy_thr=_settings.ensemble_buy_threshold,
        sell_thr=_settings.ensemble_sell_threshold,
        min_buy_votes=_settings.ensemble_min_buy_votes,
        min_sell_votes=_settings.ensemble_min_sell_votes,
        stop_pct=_settings.trade_stop_loss_pct,
        symbols=sym_list,
    )
    raw = _llm_raw(prompt, _build_system())
    if raw is None:
        return {}
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception as e:
            logger.warning("무체결 리뷰 JSON 디코딩 실패: {} | raw={}", e, raw[:300])
    # 잘림/깨진 JSON — summary·findings 만이라도 정규식으로 건져 디스코드에 반영.
    salvaged = _salvage_review_json(raw)
    if salvaged.get("summary"):
        logger.warning("무체결 리뷰 JSON 파싱 실패 — 정규식 salvage 사용 (raw {}자)", len(raw))
        return salvaged
    logger.warning("무체결 리뷰 JSON 파싱·salvage 모두 실패: {}", raw[:300])
    return {}


def _call_claude(date_str: str, trades: list[dict]) -> dict:
    _vwap_warmup_min = _settings.trade_vwap_warmup_bars * _settings.live_candle_minutes
    try:
        market = _market_snapshot()
    except Exception as exc:
        logger.warning("market_snapshot 실패: {}", exc)
        market = ""
    # 손절 평가 지시문 — 손절 정책에 맞춰 동적 생성.
    # 실효 고정손절(ATR 배수가 캡 상시초과 또는 ATR OFF)이면 ATR 튜닝 제안을 금지.
    _stop_txt, _no_atr_tuning = _stop_policy(_settings)
    if _no_atr_tuning:
        stop_eval = (
            f"6. 손절: 현재 손절은 {_stop_txt} 로 운용된다 — 의도된 고정손절 설계다. "
            f'kind="stop_loss" 매도가 있었다면 손실폭·빈도만 평가하라. '
            f"ATR·atr_stop_multiplier 관련 제안(배수 조정 등)은 절대 하지 마라(이미 백테스트로 기각됨)."
        )
    else:
        stop_eval = (
            f'6. ATR 손절: kind="stop_loss"인 매도의 손절선이 적절했나? '
            f"현재 {_stop_txt}. atr_stop_multiplier 조정 검토."
        )
    try:
        recent_context = _recent_context(date_str)
    except Exception as exc:
        logger.warning("_recent_context 실패: {}", exc)
        recent_context = ""
    prompt = USER_TEMPLATE.format(
        date=date_str, n=len(trades), trades=json.dumps(trades, ensure_ascii=False, indent=2),
        vwap_warmup_min=_vwap_warmup_min,
        market=market or "(장세 데이터 조회 실패)",
        recent_context=recent_context,
        stop_eval=stop_eval,
    )
    raw = _llm_raw(prompt, _build_system())
    if raw is None:
        return {}
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

    # 공휴일 체크: 거래일이 아니면 스킵.
    # 공용 모듈 사용 → 임시공휴일(EXTRA_HOLIDAYS)·수동 등록 휴장일까지 반영.
    if date is None:
        from stock_bot.market_calendar import is_trading_day
        if not is_trading_day(now_kst):
            logger.info("daily review skip — {} 는 휴장일", date_str)
            return None

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


if __name__ == "__main__":
    import sys

    d = sys.argv[1] if len(sys.argv) > 1 else None
    run_daily_review(d)
