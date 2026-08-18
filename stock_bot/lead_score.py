"""대장주 점수 표시 변환 — raw ↔ 100점.

raw stock_score/sector_score 는 0~1 스케일이라 사람이 크기를 가늠하기 어렵다.
화면·로그·디스코드 알림은 전부 100점으로 보여주고, 판정(밴드룰·정렬·필터)은
항상 raw 로만 한다. 예전엔 이 변환이 leader_finder.py 안에만 있어서
leader_trader 로그(바스켓 구성·밴드미달)는 raw 로 새어 나왔다 — 표시 경로가
한 군데를 공유하도록 여기로 뺀다.
"""
from __future__ import annotations


def _to_display(raw: float, ceil: float) -> float:
    """raw 점수 → 100점 표시 점수. 표시 전용(로그·대시보드·알림)이다.

    클램프(min) 때문에 상위 구간에서는 raw 대비 비율이 보존되지 않으므로,
    밴드룰·정렬·필터 비교에는 이 값을 절대 쓰지 말 것 — 그런 곳은 항상
    원본 stock_score/sector_score 를 그대로 비교해야 한다.
    """
    if not raw or not ceil:
        return 0.0
    try:
        from stock_bot.config.settings import settings as _s
        disp_max = float(getattr(_s, "lead_score_disp_max", 100.0))
    except Exception:
        disp_max = 100.0
    return round(min(disp_max, max(0.0, float(raw) / ceil * disp_max)), 1)


def to_display_stock(raw: float) -> float:
    try:
        from stock_bot.config.settings import settings as _s
        ceil = float(getattr(_s, "lead_score_disp_stock_ceil", 0.65))
    except Exception:
        ceil = 0.65
    return _to_display(raw, ceil)


def to_display_sector(raw: float) -> float:
    try:
        from stock_bot.config.settings import settings as _s
        ceil = float(getattr(_s, "lead_score_disp_sector_ceil", 0.45))
    except Exception:
        ceil = 0.45
    return _to_display(raw, ceil)
