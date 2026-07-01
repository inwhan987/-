"""장전 Claude 검수 (Opus 4.8).

스크리너 파이프라인 중간에 두 번 개입한다:

  1. `review_sector`  — market_analysis 가 고른 최강 섹터(top_sector)를 검수.
     레짐·섹터 강도(med_rs)·상승종목 비율(pos_ratio)·표본 수(count) 정합성을
     레드팀으로 점검해 부적합하면 **랭킹 안의 다른 eligible 섹터**로 교체한다.
     (없는 섹터 창작 금지 — 반드시 market_analysis 랭킹 내에서만 선택.)

  2. `review_stocks` — 스크리너가 선별한 top_n 종목을 검수. 개별 종목에
     레드플래그(기술점수 게이트 근접, 지표 모순 등)가 있으면 **스크리너가
     이미 점수 매긴 벤치(차순위 후보)** 안에서 교체한다.
     (없는 티커 창작 금지 — 반드시 스크리너 랭킹 내에서만 선택.)

두 함수 모두 실패(키 없음/네트워크/JSON 파싱/검증 실패) 시 항상 알고리즘
원본 선택을 그대로 돌려주는 fail-safe 구조다. 검수는 매매를 바꾸므로,
불확실하면 개입하지 않는다.
"""
from __future__ import annotations

import json
import os
import re

from loguru import logger

MODEL = "claude-opus-4-8"

_SYSTEM = """\
너는 한국 주식 단기 자동매매 시스템의 장전 선별 파이프라인을 감수하는 퀀트 리스크 검수관이다.

## 역할
- 알고리즘(레짐 판정 + 섹터 강도 랭킹 + 종목 스코어링)이 내놓은 선택을 **레드팀**으로 검증한다.
- 너는 종목·섹터를 새로 발굴하지 않는다. 오직 **주어진 후보 목록 안에서만** 판단한다.
- 네 기억·학습된 시세가 아니라 **전달된 숫자만** 근거로 판단한다. 목록에 없는 종목/섹터를 언급·선택하면 안 된다.

## 검수 원칙 (레드팀 체크리스트)
1. 내부 정합성 — 강도(med_rs)는 높은데 상승종목 비율(pos_ratio)이 낮으면 1~2개 급등 종목의 착시일 수 있다.
2. 표본 신뢰도 — count(표본 종목 수)가 적으면 섹터 강도의 통계적 신뢰가 낮다.
3. 레짐 정합성 — 하락장(regime=down)에서 추격 진입은 위험. 방어적으로 판단.
4. 종목 지표 모순 — 총점은 높은데 기술 지표(RSI 과열, 거래량 급감, 단기 급락)가 모순되면 감점 요인.
5. 데이터 구멍 — 재무/기술 지표에 결측·오류가 많으면 신뢰 하향.

## 개입 기준 (매우 보수적)
- 알고리즘 1순위가 명백한 결함이 있을 때만 교체한다. 애매하면 **유지(keep)**.
- 교체 시에도 반드시 후보 목록 안의 항목을 고른다.

반드시 JSON 객체 하나만 출력한다. 설명·주석·마크다운 펜스 금지.\
"""

_SECTOR_TEMPLATE = """\
## 오늘 장세 (레짐)
{regime_line}

## 섹터 강도 랭킹 (med_rs 내림차순, eligible=상승종목 비율 50%↑)
{ranking_table}

## 알고리즘이 고른 섹터
{algo_sector}  ← 랭킹 중 eligible 1순위

## 요청
위 섹터가 오늘 진입할 최강 섹터로 타당한지 검수하라.
부적합하면 랭킹 안의 **다른 eligible 섹터**로 교체를 제안하라 (eligible=false 섹터·목록에 없는 섹터 금지).

JSON 스키마:
{{"decision": "keep" | "switch",
  "chosen_sector": "<유지 시 알고리즘 섹터, 교체 시 랭킹 내 다른 eligible 섹터>",
  "reason": "<한국어 1~2문장, 근거 숫자 포함>",
  "confidence": <0.0~1.0>}}\
"""

_STOCK_TEMPLATE = """\
## 오늘 장세 (레짐)
{regime_line}

## 확정 섹터
{sector}  (선별 목표 {top_n}종목)

## 스크리너 랭킹 (총점 내림차순, selected=true 가 알고리즘 선별)
{ranked_table}

## 요청
selected=true 종목들이 오늘 실제로 매수할 종목으로 타당한지 검수하라.
개별 종목에 레드플래그가 있으면 **아래 랭킹(벤치) 안의 다른 종목**으로 교체하라 (목록에 없는 종목 금지).
최종 종목 수는 정확히 {top_n}개여야 한다.

JSON 스키마:
{{"decision": "keep" | "swap",
  "final_symbols": ["<6자리코드>", ...],   // 정확히 {top_n}개, 모두 위 랭킹 안에서
  "swaps": [{{"out": "<제외종목>", "in": "<대체종목>", "reason": "<근거>"}}],
  "reason": "<한국어 1~2문장, 근거 숫자 포함>"}}\
"""


def _client():
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("anthropic 미설치 — 장전 검수 건너뜀")
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("ANTHROPIC_API_KEY 없음 — 장전 검수 건너뜀")
        return None
    return Anthropic(api_key=key)


def _regime_line(regime: dict) -> str:
    r = regime.get("regime", "unknown")
    gap = regime.get("gap_pct", 0.0)
    tag = {"up": "상승장", "down": "하락장", "unknown": "판정불가"}.get(r, r)
    return f"{tag} (지수 이격도 평균 {gap:+.2f}%)"


def _parse_json(raw: str) -> dict | None:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception as e:
            logger.warning("장전 검수 JSON 디코딩 실패: {} | raw={}", e, raw[:300])
    return None


def _call(prompt: str) -> tuple[dict | None, float]:
    """(파싱된 JSON | None, 비용 USD)."""
    client = _client()
    if client is None:
        return None, 0.0
    cost = 0.0
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning("장전 검수 API 호출 실패: {}", e)
        return None, 0.0
    try:
        from stock_bot.costs import record_cost
        cost = record_cost("premarket_review", resp.model,
                           resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception:
        pass
    raw = resp.content[0].text if resp.content else ""
    return _parse_json(raw), cost


# ── 1) 섹터 검수 ─────────────────────────────────────────────────────────────
def review_sector(regime: dict, ranking: list[dict], algo_sector: str) -> dict:
    """market_analysis 최강 섹터 검수.

    반환: {ok, decision, chosen_sector, reason, confidence, cost_usd}
      ok=False 또는 chosen_sector 부적합 시 호출자는 algo_sector 유지.
    """
    fail = {"ok": False, "decision": "keep", "chosen_sector": algo_sector,
            "reason": "", "confidence": 0.0, "cost_usd": 0.0}
    if not ranking or not algo_sector:
        return fail

    eligible = {r["sector"] for r in ranking if r.get("eligible")}
    lines = []
    for i, r in enumerate(ranking[:10], 1):
        mark = "★" if r["sector"] == algo_sector else ("○" if r.get("eligible") else "×")
        lines.append(
            f"  {mark}{i}. {r['sector']}: med_rs={r.get('med_rs', 0):+.2f}% "
            f"avg_rs={r.get('avg_rs', 0):+.2f}% 상승비율={r.get('pos_ratio', 0)*100:.0f}% "
            f"표본={r.get('count', 0)} eligible={'Y' if r.get('eligible') else 'N'}"
        )
    prompt = _SECTOR_TEMPLATE.format(
        regime_line=_regime_line(regime),
        ranking_table="\n".join(lines),
        algo_sector=algo_sector,
    )
    result, cost = _call(prompt)
    if not result:
        return fail

    decision = str(result.get("decision", "keep")).lower()
    chosen = str(result.get("chosen_sector", "") or "").strip()
    reason = str(result.get("reason", "") or "")
    try:
        conf = float(result.get("confidence", 0.0))
    except Exception:
        conf = 0.0

    # 검증: 교체 대상은 반드시 eligible 섹터여야 하고 알고리즘 섹터와 달라야 함.
    if decision == "switch":
        if chosen not in eligible or chosen == algo_sector:
            logger.warning("섹터 검수 교체 무효 (chosen={} eligible아님/동일) — 알고리즘 유지",
                           chosen)
            return {"ok": True, "decision": "keep", "chosen_sector": algo_sector,
                    "reason": reason + " [교체안 무효→유지]", "confidence": conf,
                    "cost_usd": cost}
        return {"ok": True, "decision": "switch", "chosen_sector": chosen,
                "reason": reason, "confidence": conf, "cost_usd": cost}

    return {"ok": True, "decision": "keep", "chosen_sector": algo_sector,
            "reason": reason, "confidence": conf, "cost_usd": cost}


# ── 2) 종목 검수 ─────────────────────────────────────────────────────────────
def review_stocks(regime: dict, sector: str, ranked: list[dict],
                  top_n: int, algo_symbols: list[str]) -> dict:
    """스크리너 선별 종목 검수.

    ranked: 스크리너 JSON 블록의 랭킹 리스트
            [{sym,name,total,tech,fund,selected,sector,tech_detail,fund_detail}, ...]
    반환: {ok, decision, final_symbols, swaps, reason, cost_usd}
      ok=False 또는 검증 실패 시 호출자는 algo_symbols 유지.
    """
    fail = {"ok": False, "decision": "keep", "final_symbols": algo_symbols,
            "swaps": [], "reason": "", "cost_usd": 0.0}
    if not ranked or not algo_symbols:
        return fail

    valid_syms = {str(r.get("sym", "")).split(".")[0] for r in ranked}
    lines = []
    for r in ranked[:10]:
        sym = str(r.get("sym", "")).split(".")[0]
        mark = "★" if r.get("selected") else " "
        td = r.get("tech_detail") or {}
        fd = r.get("fund_detail") or {}
        lines.append(
            f"  {mark} {sym} {r.get('name', '')}: 총점={r.get('total', 0):.1f} "
            f"기술={r.get('tech', 0):.1f} 재무={r.get('fund', 0):.1f} "
            f"[SMA20={td.get('SMA20', '-')} RSI={td.get('RSI14', '-')} "
            f"ROC20={td.get('ROC20', '-')} 거래량={td.get('거래량', '-')} "
            f"PER={fd.get('PER', '-')} ROE={fd.get('ROE', '-')} "
            f"매출성장={fd.get('매출성장', '-')} 부채={fd.get('부채비율', '-')}]"
        )
    prompt = _STOCK_TEMPLATE.format(
        regime_line=_regime_line(regime),
        sector=sector or "(전체)",
        top_n=top_n,
        ranked_table="\n".join(lines),
    )
    result, cost = _call(prompt)
    if not result:
        return fail

    decision = str(result.get("decision", "keep")).lower()
    reason = str(result.get("reason", "") or "")
    raw_swaps = result.get("swaps") if isinstance(result.get("swaps"), list) else []
    finals = result.get("final_symbols")
    finals = [str(s).split(".")[0] for s in finals] if isinstance(finals, list) else []

    # 검증: 정확히 top_n개, 전부 랭킹 내, 중복 없음.
    ok_final = (
        len(finals) == top_n
        and len(set(finals)) == top_n
        and all(s in valid_syms for s in finals)
    )
    if not ok_final:
        logger.warning("종목 검수 결과 무효 (finals={} valid={} top_n={}) — 알고리즘 유지",
                       finals, sorted(valid_syms), top_n)
        return {"ok": True, "decision": "keep", "final_symbols": algo_symbols,
                "swaps": [], "reason": reason + " [교체안 무효→유지]", "cost_usd": cost}

    changed = set(finals) != {s.split(".")[0] for s in algo_symbols}
    return {"ok": True, "decision": "swap" if changed else "keep",
            "final_symbols": finals, "swaps": raw_swaps if changed else [],
            "reason": reason, "cost_usd": cost}
