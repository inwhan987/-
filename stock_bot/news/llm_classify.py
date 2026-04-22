"""impact CSV 에서 추출된 구(phrase) 들을 Claude LLM 으로 재분류.

임팩트 분석(`impact.py`)은 "주가와 동반 등락한" 구를 통계로 뽑지만,
그 중 *의미상* 굵직한 이벤트 키워드가 무엇인지까지는 판단하지 않는다.
이 모듈은 각 구를 Claude 에 배치로 물어 다음을 얻는다:

  - bullishness : -1 ~ +1 (의미상 호재/악재 방향과 강도)
  - is_critical : 즉각 반응할 만한 굵직한 이벤트 키워드 여부 (실적발표/
                  인수합병/상장폐지/거래정지 등)
  - category    : 실적 / 규제 / 공급 / 거버넌스 / 주주환원 / 노동 / 기타

결과는 `data/llm_phrases_{symbol}.json` 에 저장되어 `sentiment` 가중치
부여에 쓰인다.

CLI
---
  python -m stock_bot.news.llm_classify \\
      --in impact_005930_phrases.csv --symbol 005930
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path

from loguru import logger

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 25

SYSTEM = (
    "너는 한국 주식 시장 뉴스 헤드라인에 자주 등장하는 단어/구가 "
    "주가에 어떤 의미를 갖는지 판단하는 애널리스트다. "
    "오직 JSON 배열만 출력해라. 설명, 주석, 마크다운 펜스 금지."
)

USER_TEMPLATE = """다음은 한국 주식 뉴스에서 뽑힌 구(phrase) 목록이다.
각 구에 대해 아래 스키마로 평가해 JSON 배열로만 답해라.

스키마:
{{
  "phrase": 원문 그대로,
  "bullishness": -1.0 ~ +1.0 사이 실수 (호재+, 악재-, 중립은 0 근처),
  "is_critical": true 또는 false,
    // true = 기사 헤드라인에 이 구가 단 한번만 등장해도
    // 투자자가 즉시 매매 반응을 고려할 만한 굵직한 이벤트 키워드
    // (예: 어닝쇼크, 상장폐지, 거래정지, 인수합병, 자사주매입, 어닝서프라이즈)
    // 일반 시황 단어(상승/하락/확대/기대 등) 는 false.
  "category": 다음 중 하나 [실적, 규제, 공급, 거버넌스, 주주환원, 노동, 기술, 시황, 기타]
}}

주의:
- 맥락이 필요한 모호한 단어("유출", "파업")는 맥락의 보편적 방향으로 부호를 매겨라.
- 단순 "상승/하락" 같은 시황어는 critical=false, bullishness 는 +/- 0.3 수준.
- 확실한 호재/악재 이벤트는 |bullishness| >= 0.7.

구 목록:
{phrases}
"""


def _build_client():
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise SystemExit("pip install anthropic 필요") from exc
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY 환경변수 없음")
    return Anthropic(api_key=key)


def _read_phrases(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            phrase = (row.get("keyword") or "").strip()
            if not phrase:
                continue
            rows.append(
                {
                    "phrase": phrase,
                    "count": int(row.get("count", 0)),
                    "mean_ret": float(row.get("mean_ret", 0)),
                    "t_stat": float(row.get("t_stat", 0)),
                }
            )
    return rows


def _classify_batch(client, phrases: list[str], max_retries: int = 4) -> list[dict]:
    prompt = USER_TEMPLATE.format(phrases=json.dumps(phrases, ensure_ascii=False, indent=2))
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
            m = re.search(r"\[.*\]", raw, flags=re.DOTALL)
            if not m:
                raise ValueError(f"JSON 배열 못 찾음: {raw[:200]}")
            data = json.loads(m.group(0))
            if not isinstance(data, list):
                raise ValueError("배열 아님")
            return data
        except Exception as exc:
            msg = str(exc)
            is_retry = (
                "429" in msg
                or "rate_limit" in msg
                or "overloaded" in msg.lower()
                or attempt < 1  # 파싱 실패도 한번은 재시도
            )
            if is_retry and attempt < max_retries - 1:
                delay = 2 ** (attempt + 1)
                logger.warning("batch 실패 attempt={} ({}), {}s 후 재시도", attempt + 1, msg[:120], delay)
                time.sleep(delay)
                continue
            raise


def classify_all(csv_path: Path, symbol: str, out_path: Path, batch_size: int = BATCH_SIZE) -> dict:
    client = _build_client()
    rows = _read_phrases(csv_path)
    logger.info("분류 대상: {} 구 (symbol={})", len(rows), symbol)

    # phrase → impact stats 맵
    stats = {r["phrase"]: r for r in rows}
    all_phrases = [r["phrase"] for r in rows]

    results: list[dict] = []
    for i in range(0, len(all_phrases), batch_size):
        chunk = all_phrases[i : i + batch_size]
        logger.info("batch {}/{} ({} 구)", i // batch_size + 1, (len(all_phrases) + batch_size - 1) // batch_size, len(chunk))
        classified = _classify_batch(client, chunk)
        by_phrase = {c.get("phrase"): c for c in classified if isinstance(c, dict)}
        for p in chunk:
            c = by_phrase.get(p)
            if not c:
                logger.warning("빠진 구: {}", p)
                continue
            st = stats.get(p, {})
            results.append(
                {
                    "phrase": p,
                    "bullishness": float(c.get("bullishness", 0.0)),
                    "is_critical": bool(c.get("is_critical", False)),
                    "category": str(c.get("category", "기타")),
                    "count": st.get("count", 0),
                    "mean_ret": st.get("mean_ret", 0.0),
                    "t_stat": st.get("t_stat", 0.0),
                }
            )

    out = {"symbol": symbol, "model": MODEL, "count": len(results), "entries": results}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: {}", out_path)
    return out


def _summary(data: dict) -> None:
    entries = data["entries"]
    crit_pos = [e for e in entries if e["is_critical"] and e["bullishness"] > 0]
    crit_neg = [e for e in entries if e["is_critical"] and e["bullishness"] < 0]
    print(f"\n총 {len(entries)} 구 분류")
    print(f"  critical 긍정: {len(crit_pos)} / critical 부정: {len(crit_neg)}")
    print("\n[TOP critical 긍정]")
    for e in sorted(crit_pos, key=lambda x: -x["bullishness"])[:15]:
        print(f"  {e['phrase']:<20} b={e['bullishness']:+.2f} [{e['category']}] n={e['count']}")
    print("\n[TOP critical 부정]")
    for e in sorted(crit_neg, key=lambda x: x["bullishness"])[:15]:
        print(f"  {e['phrase']:<20} b={e['bullishness']:+.2f} [{e['category']}] n={e['count']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="impact CSV → Claude LLM 재분류")
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--out", default=None, help="기본: data/llm_phrases_{symbol}.json")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else Path(f"data/llm_phrases_{args.symbol}.json")
    data = classify_all(Path(args.in_path), args.symbol, out_path, batch_size=args.batch_size)
    _summary(data)


if __name__ == "__main__":
    main()
