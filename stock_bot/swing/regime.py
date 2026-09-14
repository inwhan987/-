# -*- coding: utf-8 -*-
"""레짐 필터 (명세 8-1). 지수 종가 vs MA. 청산에는 적용하지 않는다."""
from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from . import store
from .collector import IDX_CODE
from .config import SwingCfg


def index_series(date: str, ma: int):
    start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=int(ma * 1.6) + 30)).strftime("%Y%m%d")
    px = store.load_daily([IDX_CODE], start, date)
    return px.get(IDX_CODE)


def market_ok(date: str, c: SwingCfg) -> tuple[bool, float]:
    """(신규 진입 허용 여부, 사이즈 배수).

    지수 종가가 MA 위면 (True, 1.0), 아래면 (False, below_mult).
    비활성이면 (True, 1.0). 지수 데이터가 모자라면 bt_swing.engine.make_regime 과
    같이 '1(허용)' 로 본다 — 단, 로그를 남긴다.
    """
    if not c.regime_enabled:
        return True, 1.0
    df = index_series(date, c.regime_ma)
    if df is None or len(df) < c.regime_ma:
        logger.warning("regime: 지수 봉 부족({}) — 통과 처리", 0 if df is None else len(df))
        return True, 1.0
    close = float(df["close"].iloc[-1])
    m = float(df["close"].tail(c.regime_ma).mean())
    if close > m:
        return True, 1.0
    return False, c.regime_below_mult
