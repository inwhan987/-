"""임팩트 사전(lexicon) 빌드 + 런타임 로더.

`impact.py` 분석 결과 CSV 를 읽어, 통계적으로 유의한 구(phrase) → 가중치
매핑 JSON 파일을 생성한다. 런타임에서는 `load_lexicon()` 으로 불러와
`score_lexicon(text)` 으로 헤드라인 점수를 계산한다.

가중치 설계
------------
각 구 `p` 에 대해:
  weight(p) = clip(mean_ret(p) * SCALE, -1, +1)

기본 SCALE=50 → mean_ret=2% 면 weight=+1.0 (최대).
필터: count >= min_count AND |t_stat| >= min_tstat.

스코어링
--------
헤드라인에서 등장하는 구들을 longest-match 로 찾아 가중치 합산 후 평균.
최종 점수는 [-1, +1] 로 클립.

CLI
---
    python -m stock_bot.news.lexicon build \
        --in impact_005930_phrases.csv --symbol 005930 \
        --out data/lexicon_005930.json --min-count 3 --min-tstat 1.5
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

DEFAULT_SCALE = 50.0
LEXICON_DIR = Path("data")


@dataclass
class LexEntry:
    phrase: str
    weight: float  # [-1, +1]
    count: int
    mean_ret: float
    t_stat: float


def build_lexicon(
    csv_path: Path,
    min_count: int = 3,
    min_tstat: float = 1.5,
    scale: float = DEFAULT_SCALE,
    drop_single_words: bool = True,
) -> list[LexEntry]:
    """impact CSV 를 읽어 유의한 구만 추린 사전 엔트리 리스트.

    drop_single_words=True 면 공백 없는 단일 단어(예: '호재') 는 제외하고
    다중 토큰 구(예: '주주 가치 제고') 만 남긴다. 단일 단어는 오해 소지 커서 기본 배제.
    """
    out: list[LexEntry] = []
    with csv_path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phrase = row["keyword"].strip()
            if not phrase:
                continue
            if drop_single_words and " " not in phrase:
                continue
            count = int(row["count"])
            mean_ret = float(row["mean_ret"])
            t_stat = float(row["t_stat"])
            if count < min_count:
                continue
            if abs(t_stat) < min_tstat:
                continue
            w = max(-1.0, min(1.0, mean_ret * scale))
            out.append(LexEntry(phrase, w, count, mean_ret, t_stat))
    # 긴 구 우선 → longest-match 에 유리
    out.sort(key=lambda e: (-len(e.phrase), -abs(e.weight)))
    return out


def save_lexicon(entries: list[LexEntry], path: Path, symbol: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "symbol": symbol,
        "count": len(entries),
        "entries": [
            {
                "phrase": e.phrase,
                "weight": round(e.weight, 4),
                "n": e.count,
                "t": round(e.t_stat, 2),
                "mean_ret": round(e.mean_ret, 5),
            }
            for e in entries
        ],
    }
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- 런타임 ----------
_LEXICON_CACHE: dict[str, list[LexEntry]] = {}


def load_lexicon(symbol: str, base_dir: Path = LEXICON_DIR) -> list[LexEntry]:
    """심볼별 사전 로드. 없으면 빈 리스트. 프로세스 내 캐시."""
    if symbol in _LEXICON_CACHE:
        return _LEXICON_CACHE[symbol]
    path = base_dir / f"lexicon_{symbol}.json"
    if not path.exists():
        _LEXICON_CACHE[symbol] = []
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [
            LexEntry(
                phrase=e["phrase"],
                weight=float(e["weight"]),
                count=int(e.get("n", 0)),
                mean_ret=float(e.get("mean_ret", 0)),
                t_stat=float(e.get("t", 0)),
            )
            for e in data.get("entries", [])
        ]
        # 긴 구 우선 정렬 (longest-match)
        entries.sort(key=lambda e: (-len(e.phrase), -abs(e.weight)))
        _LEXICON_CACHE[symbol] = entries
        logger.info("lexicon[{}] 로드: {} 엔트리", symbol, len(entries))
        return entries
    except Exception as exc:
        logger.warning("lexicon load 실패 {}: {}", path, exc)
        _LEXICON_CACHE[symbol] = []
        return []


def score_lexicon(text: str, symbol: str) -> tuple[float, list[str]]:
    """헤드라인 → 사전 매칭 점수 ∈ [-1, +1] + 매칭된 구 목록.

    longest-match: 이미 매칭된 영역은 다시 매칭하지 않음.
    '공급 계약' 이 매칭되면 그 안의 '공급' 은 별도 카운트하지 않음.
    """
    entries = load_lexicon(symbol)
    if not entries or not text:
        return 0.0, []
    # text 에서 매칭된 문자 인덱스 마스킹
    masked = list(text)
    matched: list[tuple[str, float]] = []
    for e in entries:  # 이미 길이 내림차순
        phrase = e.phrase
        idx = 0
        while True:
            pos = "".join(masked).find(phrase, idx)
            if pos < 0:
                break
            # 겹침 체크
            if any(masked[i] == "\0" for i in range(pos, pos + len(phrase))):
                idx = pos + 1
                continue
            for i in range(pos, pos + len(phrase)):
                masked[i] = "\0"
            matched.append((phrase, e.weight))
            idx = pos + len(phrase)
    if not matched:
        return 0.0, []
    total = sum(w for _, w in matched)
    score = max(-1.0, min(1.0, total / (len(matched) * 0.5 + 0.5)))
    return score, [p for p, _ in matched]


def _cli_build(args: argparse.Namespace) -> None:
    csv_path = Path(args.in_path)
    entries = build_lexicon(
        csv_path,
        min_count=args.min_count,
        min_tstat=args.min_tstat,
        scale=args.scale,
        drop_single_words=not args.include_single_words,
    )
    out_path = Path(args.out)
    save_lexicon(entries, out_path, args.symbol)
    pos = [e for e in entries if e.weight > 0]
    neg = [e for e in entries if e.weight < 0]
    logger.info("lexicon 빌드 완료: {} (긍정 {} + 부정 {}) → {}", len(entries), len(pos), len(neg), out_path)
    print("\n[TOP 긍정 구 10]")
    for e in sorted(pos, key=lambda x: -x.weight)[:10]:
        print(f"  {e.phrase:<20} w={e.weight:+.3f} n={e.count} t={e.t_stat:+.2f}")
    print("\n[TOP 부정 구 10]")
    for e in sorted(neg, key=lambda x: x.weight)[:10]:
        print(f"  {e.phrase:<20} w={e.weight:+.3f} n={e.count} t={e.t_stat:+.2f}")


def _cli_test(args: argparse.Namespace) -> None:
    score, matched = score_lexicon(args.text, args.symbol)
    print(f"score = {score:+.3f}")
    print(f"matched = {matched}")


def main() -> None:
    ap = argparse.ArgumentParser(description="뉴스 임팩트 사전 빌드/테스트")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="impact CSV → lexicon JSON")
    b.add_argument("--in", dest="in_path", required=True, help="impact CSV 경로")
    b.add_argument("--symbol", required=True)
    b.add_argument("--out", required=True, help="출력 JSON 경로")
    b.add_argument("--min-count", type=int, default=3)
    b.add_argument("--min-tstat", type=float, default=1.5)
    b.add_argument("--scale", type=float, default=DEFAULT_SCALE)
    b.add_argument("--include-single-words", action="store_true", help="단일 단어도 포함")
    b.set_defaults(func=_cli_build)

    t = sub.add_parser("test", help="헤드라인 사전 점수 테스트")
    t.add_argument("--symbol", required=True)
    t.add_argument("--text", required=True)
    t.set_defaults(func=_cli_test)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
