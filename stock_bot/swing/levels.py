# -*- coding: utf-8 -*-
"""감시 종목 기준선 (명세 7절). 야간에 계산해 watchlist 에 박아두고 장중엔 비교만 한다."""
from __future__ import annotations

import numpy as np
import pandas as pd

from bt_swing import indicators

from .config import ExitRule, SwingCfg

# 정규장 09:00~15:30 = 390분. 3분봉 판정이면 130봉.
_SESSION_MIN = 390


def _bars_per_day(c: SwingCfg) -> float:
    return _SESSION_MIN * 60 / max(1, c.bar_sec)


def compute_levels(code: str, daily: pd.DataFrame, c: SwingCfg) -> dict:
    """일봉(원본 컬럼) → 기준선 dict. daily 는 신호일까지의 봉."""
    d = indicators.compute(daily)
    last = d.iloc[-1]
    return _levels(last, float(d["high"].tail(20).max()), c, c.exit_rule(None))


def from_scan_row(r: pd.Series, c: SwingCfg) -> dict:
    """daily_scan.scan_panel 행(이미 지표값이 있음) → 기준선."""
    return _levels(r, float(r["box_top"]), c, c.exit_rule(r.get("strategy")))


def _levels(row, box_top: float, c: SwingCfg, rule: ExitRule) -> dict:
    close = float(row["close"])
    vol_ma20 = row.get("vol_ma20")
    vol_ma20 = float(vol_ma20) if vol_ma20 is not None and np.isfinite(vol_ma20) else np.nan
    atr = row.get("atr_pct")
    atr = float(atr) if atr is not None and np.isfinite(atr) else None
    vma = row.get("value_ma20")
    vma = float(vma) if vma is not None and np.isfinite(vma) else None
    cap = row.get("mktcap_eok")
    cap = float(cap) * 1e8 if cap is not None and np.isfinite(cap) else None
    return {
        "ref_ma20": float(row["ma20"]),
        "ref_ma60": float(row["ma60"]),
        "ref_prev_close": close,               # 신호일 종가 = 다음 거래일의 전일 종가
        "ref_box_top": box_top,                # 신호일 포함 20일 고가
        "ref_atr_pct": atr,
        # 3분봉 1개당 평균 거래량 — 20일 평균 일거래량 / 하루 봉 수
        "ref_avg_bar_vol": (vol_ma20 / _bars_per_day(c)) if np.isfinite(vol_ma20) else None,
        # 진입가를 모르는 밤에는 전일 종가 기준 잠정치. 체결 시 entry_px 로 다시 계산.
        "stop_px": round(close * (1 - rule.stop_pct), 2),
        "tp_px": round(close * (1 + rule.tp_pct), 2) if rule.tp_pct > 0 else None,
        "ref_value_ma20": vma,
        "ref_mktcap": cap,
    }


def entry_levels(entry_px: float, rule: ExitRule) -> tuple[float, float | None]:
    """체결가 기준 손절·익절가. 익절 없음(tp_pct<=0)이면 tp None."""
    return entry_px * (1 - rule.stop_pct), (entry_px * (1 + rule.tp_pct) if rule.tp_pct > 0 else None)
