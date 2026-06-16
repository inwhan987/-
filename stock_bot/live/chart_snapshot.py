"""차트 탭용 분봉 스냅샷 — 순수 표시(presentation) 계층. 거래 로직 불변.

봇(runner=스톡봇, leader_trader=대장주)이 매 틱 이미 계산하는 N분봉
(get_minute_ohlcv_today 결과, newest-first)을 세 컨테이너가 공유 마운트하는
data/charts/ 에 작은 JSON 으로 떨군다. 웹(별 프로세스)이 이를 읽어 KIS 추가
호출 없이 캔들차트를 그린다.

거래에 절대 영향을 주면 안 되므로 모든 쓰기는 try/except 로 감싸 실패를
조용히 무시한다. 호출측 제어 흐름을 바꾸지 않는다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _ROOT / "data" / "charts"


def _safe_code(symbol: str) -> str:
    """파일명용 bare 코드. '005930.KS' → '005930', 영숫자만 유지."""
    bare = str(symbol).split(".")[0]
    return "".join(ch for ch in bare if ch.isalnum()) or "unknown"


def write_snapshot(
    symbol: str, interval_min: int, bars: list[dict[str, Any]], *, source: str
) -> None:
    """N분봉 스냅샷을 data/charts/{code}.json 에 원자적으로 기록.

    bars: get_minute_ohlcv_today 반환 형태(newest-first, 키 date/time/open/high/
    low/close/volume). source: "live"(스톡봇) | "leader"(대장주).
    """
    try:
        if not bars:
            return
        code = _safe_code(symbol)
        _CHART_DIR.mkdir(parents=True, exist_ok=True)
        date = bars[0].get("date", "")
        payload = {
            "symbol": code,
            "interval_min": int(interval_min),
            "source": source,
            "date": date,
            "updated_at": time.time(),
            # newest-first 그대로 저장 — 웹에서 정렬/가공
            "bars": [
                {
                    "t": b.get("time", ""),
                    "o": float(b.get("open", 0)),
                    "h": float(b.get("high", 0)),
                    "l": float(b.get("low", 0)),
                    "c": float(b.get("close", 0)),
                    "v": int(b.get("volume", 0) or 0),
                }
                for b in bars
            ],
        }
        tmp = _CHART_DIR / f".{code}.tmp"
        final = _CHART_DIR / f"{code}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)  # 원자적 교체 — 웹이 반쪽 파일을 읽지 않게
    except Exception:
        # 표시용 — 어떤 실패도 거래 틱에 영향 주지 않는다
        pass
