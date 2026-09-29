"""dart_hist.py 가 읽을 종목 목록을 만든다 — 게이트 통과 시그널의 distinct 종목.

CI 에는 swing.db 가 없으므로 목록을 파일로 떠서 커밋해둔다.

순환 우려는 없다: ms1.gate() 는 전략 원점수·거래대금·시총·ATR·레짐만 본다. 축점수
(quality/growth)를 쓰지 않으므로, 축을 채워도 이 종목 집합은 변하지 않는다. 그리고
axes.compute() 의 백분위는 "그날 게이트 통과 시그널을 code 로 중복제거한 집합" 안에서
계산되므로, 이 목록이 정확히 필요한 모집단이다.

    python scripts/research/dart_hist_codes.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import resistance_exit as RE  # noqa: E402

OUT = Path(__file__).with_name("dart_hist_codes.json")


def main() -> None:
    sig, _P, _ent, _avail = RE.load()
    codes = sorted({str(c).zfill(6) for c in sig["code"].unique()})
    OUT.write_text(json.dumps(codes, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(codes)}종목 → {OUT.name}")
    print(f"기간 {sig['date'].min()} ~ {sig['date'].max()} / 행 {len(sig)}")


if __name__ == "__main__":
    main()
