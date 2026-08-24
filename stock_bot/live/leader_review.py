"""대장주봇 전용 장마감 리뷰 (스톡봇/앙상블 리뷰와 분리).

앙상블 리뷰(review.py)는 '체결 로그'가 곧 데이터지만, 대장주봇은 하루 체결이
0~1건이라 체결만 보면 볼 게 없다. 정보의 대부분은 **'왜 안 샀나'** 에 있다.
그래서 이 리뷰는 체결 리뷰가 아니라 **깔때기(funnel) 리뷰**로 만든다.

  1. 선별  — 오늘 섹터 순위/점수, 재선별로 1등 섹터가 바뀌었는지
  2. 감시  — 감시 바스켓, 보류/신호스킵/미진입 건수와 사유 분포
  3. 체결  — 진입·청산 왕복, net%, **MFE/MAE**
  4. 반사실 — 감시했는데 안 산 종목의 당일 최대 상승폭(놓친 폭)

## MFE/MAE 를 넣는 이유
08-24 리뷰가 "+0.18% 에 그쳐 엣지 소멸"이라고 단정했는데, 그 포지션이 장중에
+2% 까지 갔다 되밀린 건지 하루종일 정체였는지는 리포트로 알 수 없었다. 둘은
처방이 정반대다(전자=청산 문제, 후자=진입 문제). 진입가·이후 고가·저가만
있으면 갈리는 문제라 분봉에서 계산해 넣는다.

## LLM 호출 주기
대장주는 표본이 하루 0~2건이라 매일 LLM 해석을 돌리면 단발 표본으로 규칙을
만들자는 제안이 나온다(08-24 '진행부진 조기청산' = 이미 기각된 시간손절).
그래서 매일은 **기계적 팩트시트**만 만들고, LLM 해석은 금요일 또는 미리뷰
체결이 N건 누적됐을 때만 돈다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from stock_bot.config import settings as _settings
from stock_bot.market_calendar import KST as _KST
from stock_bot.notify import notify
from stock_bot.storage import ENGINE, ReviewLog, TradeLog, record_review

_ROOT = Path(__file__).resolve().parents[2]
_PICKS_DIR = _ROOT / "data" / "leader_picks"
_STATE_DIR = _ROOT / "data" / "leader_trade_state"
_LIVE_LOG = _ROOT / "logs" / "stock_leader.log"

_LEADER_STRATEGIES = ("leader_vwap_touch", "leader_pullback")
_BUY_COMM = 0.00015    # leader_trader.py:50-51 과 동일해야 수치가 맞다
_SELL_COMM = 0.00195

# 이미 검증 후 기각된 방향 — 프롬프트에 고정 주입해 재탕 제안을 막는다.
# 이게 없으면 리뷰가 매번 시간손절/조기청산/트레일링을 새 아이디어처럼 제안한다.
_REJECTED = """\
## 이미 백테스트로 검증 후 기각된 제안 (다시 제안하지 마라)
- 시간손절 / 진행부진 조기청산 / 보유시간 상한 — 스윕 전멸
- 트레일링 스톱, ATR 배수 손절, ATR 기반 사이징 — 기각
- 서킷브레이커, 오버나이트 청산, 시간대 컷 — 기각
- 대장주 청산규칙 전수 스윕(2026-08-18) — 현행 고정 규칙이 최적
- 체급별 지표 파라미터 분리(2026-07-15) — 기각
- 섹터 점수가 빠졌다고 보유 포지션 갈아타기 — 진입 조건 자체가 '눌림목'이라
  진입 직후가 점수 최저점이다. 갈아타기는 곧 최저점 매도 규칙이 된다.
※ 진입·청산 레이어는 스윕이 끝났다. 남은 여지는 **선별·감시 레이어**뿐이다.
"""

_SYSTEM = """\
너는 한국 주식 단기 자동매매 시스템을 운용·개선해온 퀀트 트레이더다.
지금 평가 대상은 **대장주봇(leader)** 하나뿐이다. 앙상블(스톡봇)은 별도 리뷰가
있으니 언급하지 마라.

## 대장주 전략 요약
- 매일 09:30 선별: 자격필터 → 섹터 점수화 → 상위 섹터의 1~3등 종목을 감시
  바스켓에 편입 (장중 재선별로 섹터 순위를 갱신)
- 진입: 감시종목 3분봉의 VWAP 눌림 + **회복확인** 후 매수 (눌린 것을 산다)
- 청산: +4% 익절 / 고정 손절 / 마감 청산
- 하루 체결 0~2건이 정상이다. 체결 0건은 그 자체로 실패가 아니다.

## 리뷰 원칙
- 칭찬·일반론 금지. 단발 표본으로 규칙을 만들지 마라.
- **깔때기로 읽어라**: 선별 → 감시 → 신호 → 체결 중 어디서 몇 개가 걸러졌나.
  체결이 0이어도 '감시 N종목 중 눌림 M회, 회복확인 실패 K회'가 진짜 데이터다.
- MFE(최대 유리 이동)/MAE(최대 불리 이동)를 먼저 보라. 실현 수익이 작아도
  MFE 가 컸다면 청산 문제, MFE 도 작았다면 진입(종목 선정) 문제다.
- 반사실(감시했는데 안 산 종목의 당일 최대 상승폭)이 반복해서 크면 신호가
  과보수적이라는 신호다. 반대면 미진입이 옳았다는 근거다.

""" + _REJECTED + """
반드시 JSON 객체 하나만 출력한다. 설명, 주석, 마크다운 펜스 금지.
{
  "summary": "1~3문장 한국어 총평",
  "findings": ["구체적 패턴/문제. 깔때기 어느 단계인지 명시", "..."],
  "suggestions": ["선별·감시·신호 레이어 제안(파라미터명·현재값·방향)", "..."]
}
findings/suggestions 각 0~5개. 근거가 부족하면 빈 배열을 반환하고 summary 에
'표본 부족'이라고 쓰는 게 낫다. 억지 제안 금지."""


def _bare(code: str) -> str:
    return str(code).split(".")[0].strip()


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ────────────────────────────── 1. 선별 ──────────────────────────────
def _stage_selection(date: str) -> list[str]:
    out = ["## 1. 선별"]
    base = _load_json(_PICKS_DIR / f"{date}.json")
    if not base:
        out.append("- 정본 picks 없음 (선별 미실행 또는 실패)")
        return out
    leaders = base.get("leaders") or []
    out.append(
        f"- 정본 {base.get('selected_at', '?')} · 섹터후보 {len(leaders)}개"
        f" · flow {base.get('flow_tier', '?')}"
    )
    for L in leaders[:5]:
        out.append(
            f"  · {L.get('sector', '?')} 섹터점수 {float(L.get('sector_score_100') or 0):.1f}"
            f" · 1등 {L.get('name', '?')} {float(L.get('change_pct') or 0):+.2f}%"
        )
    # 재선별 결과 — 1등 섹터가 바뀌었는지가 선별 안정성의 핵심 지표다.
    rev = _load_json(_PICKS_DIR / f"{date}_reval.json")
    if rev:
        rl = rev.get("leaders") or []
        top_now = rl[0].get("sector", "?") if rl else "?"
        top_base = leaders[0].get("sector", "?") if leaders else "?"
        changed = "변동없음" if top_now == top_base else "교체"
        out.append(
            f"- 최종 재선별 {rev.get('selected_at', '?')} · 1등 섹터 "
            f"{top_base} → {top_now} ({changed})"
        )
    hist = _PICKS_DIR / f"{date}_reval_history.jsonl"
    if hist.exists():
        try:
            n = sum(1 for ln in hist.read_text(encoding="utf-8").splitlines() if ln.strip())
            out.append(f"- 재선별 실행 {n}회")
        except Exception:
            pass
    return out


# ────────────────────────────── 2. 감시 ──────────────────────────────
_RE_SKIP = re.compile(r"leader_trader: (.+?) (보류|신호 스킵|미진입) [-—] (.+?)\s*$")
_RE_NOADD = re.compile(r"leader_trader: 섹터 미추가 [-—] (.+?) . (.+?)\s*$")
_RE_NUM = re.compile(r"[0-9][0-9,.]*%?")


def _leader_log_lines(date: str) -> list[str]:
    """그날 대장주 로그 라인. 스냅샷(data/logs/leader/{date}.log)이 있으면 그걸,
    없으면(당일 리뷰) 라이브 로그에서 해당 날짜 라인만 추린다."""
    snap = _ROOT / "data" / "logs" / "leader" / f"{date}.log"
    for p in (snap, _LIVE_LOG):
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = [ln for ln in txt.splitlines() if ln.startswith(date)]
        if lines:
            return lines
    return []


def _stage_watch(date: str, log_lines: list[str]) -> tuple[list[str], list[dict]]:
    """감시 단계 요약 + 감시 종목 목록(반사실 계산용)."""
    out = ["## 2. 감시·신호"]
    st = _load_json(_STATE_DIR / f"{date}.json")
    baskets = st.get("sector_baskets") or {}
    watched: list[dict] = []
    for sec, members in baskets.items():
        for m in members or []:
            watched.append({
                "code": _bare(m.get("code", "")),
                "name": m.get("name", ""),
                "sector": sec,
            })
    out.append(
        f"- 감시 {len(baskets)}섹터 / {len(watched)}종목"
        f" · 활성섹터 {st.get('active_sector_name', '?')}"
    )

    # 신호 판정 카운트 — '왜 안 샀나' 가 이 리뷰의 핵심 데이터다.
    buckets: dict[str, list[str]] = {"보류": [], "신호 스킵": [], "미진입": []}
    noadd: list[str] = []
    for ln in log_lines:
        m = _RE_SKIP.search(ln)
        if m:
            buckets.setdefault(m.group(2), []).append(m.group(3))
            continue
        m = _RE_NOADD.search(ln)
        if m:
            noadd.append(f"{m.group(1)} · {m.group(2)}")
    any_hit = False
    for label, reasons in buckets.items():
        if not reasons:
            continue
        any_hit = True
        # 사유별 빈도 — 같은 사유가 반복되면 그게 곧 병목이다.
        # 가격·퍼센트 숫자는 N 으로 치환해야 같은 사유가 하나로 묶인다.
        freq: dict[str, int] = {}
        for r in reasons:
            key = _RE_NUM.sub("N", r.split("—")[0]).strip()[:70]
            freq[key] = freq.get(key, 0) + 1
        top = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:3]
        out.append(
            f"- {label} {len(reasons)}건 · 주요사유: "
            + " / ".join(f"{k}({c})" for k, c in top)
        )
    if not any_hit:
        out.append("- 신호 판정 로그 없음 (감시 미가동 또는 로그 미확보)")
    if noadd:
        out.append(f"- 섹터 미추가 {len(noadd)}건 (예: {noadd[0]})")
    return out, watched


# ────────────────────────────── 3. 체결 ──────────────────────────────
def _leader_trades(date: str) -> list[dict]:
    start = datetime.strptime(date, "%Y-%m-%d")
    end = start + timedelta(days=1)
    with Session(ENGINE) as s:
        rows = s.scalars(
            select(TradeLog)
            .where(TradeLog.ts >= start)
            .where(TradeLog.ts < end)
            .order_by(TradeLog.ts)
        ).all()
        out = []
        for r in rows:
            if r.strategy not in _LEADER_STRATEGIES:
                continue
            try:
                br = json.loads(r.broker_response) if r.broker_response else {}
            except Exception:
                br = {}
            if isinstance(br, dict) and br.get("dry_run"):
                continue
            kst = r.ts.replace(tzinfo=timezone.utc).astimezone(_KST)
            out.append({
                "ts": kst.strftime("%H:%M:%S"),
                "symbol": _bare(r.symbol),
                "side": (r.side or "").upper(),
                "price": float(r.price or 0),
                "strategy": r.strategy,
                "reason": r.reason or "",
            })
        return out


def _round_trips(trades: list[dict]) -> list[dict]:
    """종목별 BUY→SELL 순서쌍. 대장주는 종목당 1왕복이 정상이라 단순 큐로 충분."""
    pend: dict[str, dict] = {}
    rts: list[dict] = []
    for t in trades:
        sym = t["symbol"]
        if t["side"] == "BUY":
            pend[sym] = t
            continue
        b = pend.pop(sym, None)
        if not b:
            continue
        bp, sp = b["price"], t["price"]
        cost = bp * (1 + _BUY_COMM)
        net = ((sp * (1 - _SELL_COMM) - cost) / cost * 100) if cost else 0.0
        rts.append({
            "symbol": sym, "entry_ts": b["ts"], "exit_ts": t["ts"],
            "entry": bp, "exit": sp, "net_pct": net,
            "exit_reason": t["reason"][:80], "strategy": b["strategy"],
        })
    # 미청산(오버나이트)은 정상이 아니다 — 묻히지 않게 그대로 남긴다.
    for sym, b in pend.items():
        rts.append({
            "symbol": sym, "entry_ts": b["ts"], "exit_ts": "미청산",
            "entry": b["price"], "exit": 0.0, "net_pct": 0.0,
            "exit_reason": "미청산", "strategy": b["strategy"],
        })
    return rts


# ─────────────────────── 4. MFE/MAE · 반사실 ───────────────────────
def _hi_lo_after(bars: list[dict], hhmmss: str | None) -> tuple[float, float]:
    """hhmmss(HH:MM:SS) 이후 봉들의 (고가, 저가). None 이면 전체 구간."""
    cut = (hhmmss or "").replace(":", "")[:6]
    hi = lo = 0.0
    for b in bars:
        t = str(b.get("time") or "")
        if cut and t and t < cut:
            continue
        try:
            h = float(b.get("high") or 0)
            l = float(b.get("low") or 0)
        except Exception:
            continue
        if h:
            hi = max(hi, h)
        if l:
            lo = l if not lo else min(lo, l)
    return hi, lo


def _stage_bars(broker, round_trips: list[dict], watched: list[dict],
                max_counterfactual: int = 8) -> list[str]:
    """체결 MFE/MAE + 미진입 감시종목 반사실.

    `get_minute_ohlcv_today` 는 이름 그대로 **오늘** 분봉만 준다. 과거 날짜로
    리뷰를 돌리면 엉뚱한 날 봉이 붙으므로 호출부에서 당일일 때만 넘긴다.
    """
    fills = ["## 3. 체결 · MFE/MAE"]
    if not round_trips:
        fills.append("- 체결 0건")

    def _bars(code: str) -> list[dict]:
        try:
            return broker.get_minute_ohlcv_today(code, interval_min=3) or []
        except Exception as exc:
            logger.warning("leader_review: {} 분봉 조회 실패 {}", code, exc)
            return []

    entered = set()
    for rt in round_trips:
        entered.add(rt["symbol"])
        e = rt["entry"] or 0
        hi, lo = _hi_lo_after(_bars(rt["symbol"]), rt["entry_ts"])
        mfe = (hi - e) / e * 100 if (e and hi) else 0.0
        mae = (lo - e) / e * 100 if (e and lo) else 0.0
        fills.append(
            f"- {rt['symbol']} {rt['entry_ts']}→{rt['exit_ts']} "
            f"진입 {e:,.0f} 청산 {rt['exit']:,.0f} · net {rt['net_pct']:+.2f}% "
            f"· MFE {mfe:+.2f}% / MAE {mae:+.2f}% · {rt['exit_reason']}"
        )
        if mfe >= 2.0 and rt["net_pct"] < 0.5:
            fills.append("    ↳ MFE 큼 + 실현 작음 = 청산 문제(진입 자체는 맞았다)")
        elif hi and mfe < 1.0:
            fills.append("    ↳ MFE 도 작음 = 진입/선정 문제(청산 탓이 아니다)")

    cf = ["## 4. 반사실 (미진입 감시종목 · 시가 대비 당일 고저)"]
    rest = [w for w in watched if w["code"] not in entered][:max_counterfactual]
    if not rest:
        cf.append("- 미진입 감시종목 없음")
    for w in rest:
        bars = _bars(w["code"])
        if not bars:
            continue
        base = 0.0
        for b in reversed(bars):   # newest-first → 가장 이른 봉의 시가가 기준
            try:
                base = float(b.get("open") or 0)
            except Exception:
                base = 0.0
            if base:
                break
        hi, lo = _hi_lo_after(bars, None)
        if not base or not hi:
            continue
        cf.append(
            f"- {w['name']}({w['code']}) {w['sector']}: "
            f"최대 {(hi - base) / base * 100:+.2f}% / 최저 {(lo - base) / base * 100:+.2f}%"
        )
    return fills + [""] + cf


# ────────────────────────────── LLM ──────────────────────────────
def _llm_due(date: str) -> tuple[bool, str]:
    """LLM 해석을 돌릴 날인가? 금요일이거나, 직전 LLM 리뷰 이후 체결 N건 누적."""
    d = datetime.strptime(date, "%Y-%m-%d")
    need = max(1, int(getattr(_settings, "leader_review_llm_min_trades", 5)))
    if d.weekday() == 4:
        return True, "금요일 주간 해석"
    with Session(ENGINE) as s:
        rows = s.scalars(
            select(ReviewLog)
            .where(ReviewLog.kind == "leader")
            .where(ReviewLog.date <= date)
            .order_by(ReviewLog.date.desc())
            .limit(30)
        ).all()
        since = None
        for r in rows:
            try:
                if json.loads(r.raw_context or "{}").get("llm"):
                    since = r.date
                    break
            except Exception:
                continue
        start = datetime.strptime(since, "%Y-%m-%d") if since else d - timedelta(days=30)
        buys = s.scalars(
            select(TradeLog).where(TradeLog.ts >= start).where(TradeLog.ts < d + timedelta(days=1))
        ).all()
    n = sum(1 for r in buys
            if r.strategy in _LEADER_STRATEGIES and (r.side or "").upper() == "BUY")
    if n >= need:
        return True, f"미리뷰 체결 {n}건 누적(≥{need})"
    return False, f"표본 부족({n}/{need}) — 팩트시트만"


def _call_llm(date: str, facts: str) -> dict:
    from stock_bot.live.review import _llm_raw, _salvage_review_json

    prompt = (
        f"오늘({date} KST) 대장주봇 깔때기 팩트시트다.\n\n{facts}\n\n"
        "위 데이터만으로 평가하라. 없는 수치를 지어내지 마라."
    )
    raw = _llm_raw(prompt, _SYSTEM, source="leader_review")
    if raw is None:
        return {}
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return _salvage_review_json(raw)


# ────────────────────────────── 엔트리 ──────────────────────────────
def run_leader_review(date: str | None = None, broker=None) -> int | None:
    """대장주 깔때기 리뷰를 만들어 ReviewLog(kind='leader') 에 저장. row id 반환."""
    if not getattr(_settings, "leader_review_enabled", True):
        logger.info("leader review skip — LEADER_REVIEW_ENABLED=false")
        return None
    now_kst = datetime.now(tz=_KST)
    today = now_kst.strftime("%Y-%m-%d")
    date_str = date or today
    if date is None:
        from stock_bot.market_calendar import is_trading_day
        if not is_trading_day(now_kst):
            logger.info("leader review skip — {} 는 휴장일", date_str)
            return None

    sections: list[str] = []
    try:
        sections += _stage_selection(date_str) + [""]
    except Exception as exc:
        logger.warning("leader_review 선별단계 실패: {}", exc)
    watched: list[dict] = []
    try:
        w_lines, watched = _stage_watch(date_str, _leader_log_lines(date_str))
        sections += w_lines + [""]
    except Exception as exc:
        logger.warning("leader_review 감시단계 실패: {}", exc)

    rts = _round_trips(_leader_trades(date_str))
    if date_str == today:
        # 분봉은 당일치만 조회 가능 — 과거 날짜 리뷰에선 이 단계를 건너뛴다.
        if broker is None:
            try:
                from stock_bot.broker import KISBroker
                broker = KISBroker()
            except Exception as exc:
                logger.warning("leader_review: 브로커 생성 실패 {} — 분봉 분석 생략", exc)
        if broker is not None:
            try:
                sections += _stage_bars(broker, rts, watched)
            except Exception as exc:
                logger.warning("leader_review 분봉단계 실패: {}", exc)
    else:
        sections.append(f"## 3~4. 분봉 분석 생략 (과거 날짜 {date_str} — 당일 분봉만 조회 가능)")
    facts = "\n".join(sections).strip()

    due, why = _llm_due(date_str)
    result: dict = {}
    if due:
        try:
            result = _call_llm(date_str, facts) or {}
        except Exception as exc:
            logger.exception("leader_review LLM 실패: {}", exc)
    summary = str(result.get("summary", "")).strip() or f"{date_str} 대장주 팩트시트 ({why})"
    findings = result.get("findings") if isinstance(result.get("findings"), list) else []
    suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []

    rid = record_review(
        date=date_str,
        trades_count=len(rts),
        summary=summary,
        findings=findings,
        suggestions=suggestions,
        raw_context=json.dumps({"llm": bool(due and result), "facts": facts},
                               ensure_ascii=False),
        kind="leader",
    )
    logger.info("leader review 저장 id={} 왕복={} llm={} ({})",
                rid, len(rts), bool(due and result), why)

    lines = [f"🎯 **{date_str} 장마감 리뷰 · 대장주** (왕복 {len(rts)}건)", "", summary, "", facts]
    if findings:
        lines += ["", "**발견 사항**"] + [f"• {f}" for f in findings[:5]]
    if suggestions:
        lines += ["", "**제안 조정**"] + [f"• {s}" for s in suggestions[:5]]
    notify("\n".join(lines))
    return rid


if __name__ == "__main__":
    import sys
    run_leader_review(sys.argv[1] if len(sys.argv) > 1 else None)
