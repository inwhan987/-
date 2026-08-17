# -*- coding: utf-8 -*-
"""
AVWAP 섀도 프로브 — 매매 로직 무간섭 관찰자.

목적
  1) 앵커드 VWAP(급등 시작봉부터 누적)을 임계값 2x/3x/5x 동시에 계산
  2) 각 변형에서 vwap_touch 신호가 몇 번 발생하는지 세션 VWAP(대조군)과 비교
  3) 부수적으로 장대양봉컷이 신호를 얼마나 죽이는지 퍼널 집계
  4) 진입가-손절가 갭을 매 신호마다 기록 (R:R 실측)

원칙
  - 매매 로직을 절대 건드리지 않는다. 순수 관찰자.
  - 어떤 예외도 밖으로 던지지 않는다. 프로브가 죽어도 봇은 계속 돈다.
  - 판정식은 _signal_vwap_touch(leader_trader.py:986-989)와 동일하게 맞춘다.

사용
    from stock_bot.live.avwap_probe import AvwapProbe

    probe = AvwapProbe()          # 앱 시작 시 1회 생성

    # 3분봉 종가 스캔 루프 안, 종목별로 한 줄 추가
    probe.observe(code=code, name=name, bars=bars,
                  base_vol_per_bar=base_vol,   # 없으면 None (내부 추정)
                  meta={"sector": sector, "rank": rank,
                        "stock_score": score, "entered": did_enter})
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

# ── 기본 설정 (settings.py 로 옮겨도 됨) ──────────────────
AVWAP_PROBE_ENABLED   = True
AVWAP_PROBE_MULTS     = (2.0, 3.0, 5.0)   # 앵커 판정 거래량 배수 후보
AVWAP_PROBE_DIR       = "data/avwap_probe"
AVWAP_MIN_BASE_BARS   = 5                 # 내부 베이스라인 추정 최소 봉수

# _signal_vwap_touch 와 동일하게 유지할 것
VWAP_TOL_PCT          = 0.3
BAR_RANGE_PCT         = 1.5
STOP_BUF_PCT          = 1.5


# ── 봉 접근 헬퍼 — dict / 객체 / 튜플 모두 수용 ────────────
_FIELD_ALIASES = {
    "open":   ("open", "o", "stck_oprc", "opn", "open_price"),
    "high":   ("high", "h", "stck_hgpr", "high_price"),
    "low":    ("low", "l", "stck_lwpr", "low_price"),
    "close":  ("close", "c", "stck_prpr", "close_price", "cur"),
    "volume": ("volume", "v", "vol", "cntg_vol", "acml_vol_delta"),
    "ts":     ("ts", "time", "dt", "datetime", "stck_cntg_hour", "bar_time"),
}


def _f(bar, field, default=None):
    """봉에서 필드 하나를 꺼낸다. dict / attr / index 순으로 시도."""
    names = _FIELD_ALIASES.get(field, (field,))
    if isinstance(bar, dict):
        for n in names:
            if n in bar and bar[n] is not None:
                return bar[n]
        return default
    for n in names:
        v = getattr(bar, n, None)
        if v is not None:
            return v
    if isinstance(bar, (list, tuple)):
        idx = {"open": 0, "high": 1, "low": 2, "close": 3, "volume": 4}.get(field)
        if idx is not None and len(bar) > idx:
            return bar[idx]
    return default


def _num(x, default=0.0):
    try:
        v = float(x)
        return v if v == v else default          # NaN 방어
    except (TypeError, ValueError):
        return default


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# ── 코어 계산 ────────────────────────────────────────────
def _typical(bar):
    return (_num(_f(bar, "high")) + _num(_f(bar, "low")) + _num(_f(bar, "close"))) / 3.0


def session_vwap_series(bars):
    """09:00부터 누적한 세션 VWAP 시계열. 현행 _signal_vwap_touch 기준."""
    out, pv, vv = [], 0.0, 0.0
    for b in bars:
        v = _num(_f(b, "volume"))
        pv += _typical(b) * v
        vv += v
        out.append(pv / vv if vv > 0 else _num(_f(b, "close")))
    return out


def anchor_index(bars, mult, base_vol_per_bar=None, min_base_bars=AVWAP_MIN_BASE_BARS):
    """
    당일 최초로 (거래량 >= base × mult) AND (종가 > 시가) 인 봉의 인덱스.

    base_vol_per_bar 가 주어지면 그것을 쓴다(권장 — leader_finder 의
    avg_value_5d 에서 유도). 없으면 직전 봉들의 중앙값으로 추정한다.
    중앙값을 쓰는 이유: 평균은 급등 스파이크에 부풀려져 앵커를 놓친다.
    """
    prior = []
    for i, b in enumerate(bars):
        vol = _num(_f(b, "volume"))
        base = base_vol_per_bar
        if base is None:
            if len(prior) < min_base_bars:
                prior.append(vol)
                continue
            base = _median(prior)
        if base and base > 0 and vol >= base * mult:
            if _num(_f(b, "close")) > _num(_f(b, "open")):
                return i
        prior.append(vol)
    return None


def avwap_series(bars, start_idx):
    """start_idx 부터 누적한 앵커드 VWAP. 그 이전 구간은 None."""
    out = [None] * len(bars)
    if start_idx is None or start_idx >= len(bars):
        return out
    pv = vv = 0.0
    for i in range(start_idx, len(bars)):
        v = _num(_f(bars[i], "volume"))
        pv += _typical(bars[i]) * v
        vv += v
        out[i] = pv / vv if vv > 0 else _num(_f(bars[i], "close"))
    return out


def touch_signal(bars, vwaps, j, vwap_tol=VWAP_TOL_PCT,
                 bar_range_pct=BAR_RANGE_PCT, stop_buf=STOP_BUF_PCT):
    """
    j번째 봉에서 vwap_touch 신호를 판정한다.
    _signal_vwap_touch 와 동일한 3조건 + 장대양봉컷.

    반환: 각 단계 통과 여부와 갭까지 담은 dict (퍼널 집계용)
    """
    res = dict(trend=False, touch=False, reclaim=False,
               raw_signal=False, bar_ok=False, signal=False,
               bar_range=None, gap_pct=None, entry=None, stop=None, vwap=None)
    if j <= 0 or j >= len(bars):
        return res
    vw, vw_prev = vwaps[j], vwaps[j - 1]
    if vw is None or vw_prev is None:
        return res

    hi = _num(_f(bars[j], "high"))
    lo = _num(_f(bars[j], "low"))
    cl = _num(_f(bars[j], "close"))
    cl_prev = _num(_f(bars[j - 1], "close"))
    if lo <= 0 or cl <= 0:
        return res

    res["vwap"] = vw
    res["trend"]   = cl_prev > vw_prev                      # (a) 상승추세
    res["touch"]   = lo <= vw * (1 + vwap_tol / 100.0)      # (b) 터치
    res["reclaim"] = cl >= vw                               # (c) 회복
    res["raw_signal"] = res["trend"] and res["touch"] and res["reclaim"]

    br = (hi - lo) / lo * 100.0                             # 장대양봉컷 (저가 기준)
    res["bar_range"] = br
    res["bar_ok"] = br <= bar_range_pct
    res["signal"] = res["raw_signal"] and res["bar_ok"]

    stop = lo * (1 - stop_buf / 100.0)                      # ref = lows[j]
    res["entry"] = cl
    res["stop"] = stop
    res["gap_pct"] = (cl - stop) / cl * 100.0
    return res


# ── 프로브 ───────────────────────────────────────────────
class AvwapProbe:
    def __init__(self, mults=AVWAP_PROBE_MULTS, out_dir=AVWAP_PROBE_DIR,
                 enabled=AVWAP_PROBE_ENABLED, vwap_tol=VWAP_TOL_PCT,
                 bar_range_pct=BAR_RANGE_PCT, stop_buf_pct=STOP_BUF_PCT):
        self.mults = tuple(mults)
        self.out_dir = out_dir
        self.enabled = enabled
        self.vwap_tol = vwap_tol
        self.bar_range_pct = bar_range_pct
        self.stop_buf_pct = stop_buf_pct
        self._lock = threading.Lock()
        self._seen = set()          # (date, code, bar_idx) 중복 방지

    # -- public ------------------------------------------------------
    def observe(self, code, bars, name=None, base_vol_per_bar=None, meta=None):
        """3분봉 종가 스캔 루프에서 종목당 1회 호출. 절대 예외를 던지지 않는다."""
        if not self.enabled:
            return None
        try:
            return self._observe(code, bars, name, base_vol_per_bar, meta)
        except Exception:                                    # noqa: BLE001
            return None

    # -- internal ----------------------------------------------------
    def _observe(self, code, bars, name, base_vol_per_bar, meta):
        if not isinstance(bars, (list, tuple)) or len(bars) < 2:
            return None
        j = len(bars) - 1                                    # 방금 닫힌 봉
        # 최소 유효성 — 쓰레기 입력이 로그를 오염시키지 않게
        if _num(_f(bars[j], "close")) <= 0 or _num(_f(bars[j], "low")) <= 0:
            return None
        now = datetime.now(KST)
        day = now.strftime("%Y-%m-%d")

        key = (day, str(code), j)
        with self._lock:
            if key in self._seen:
                return None
            self._seen.add(key)

        rec = {
            "date": day,
            "ts": now.strftime("%H:%M:%S"),
            "bar_idx": j,
            "code": str(code),
            "name": name,
            "close": _num(_f(bars[j], "close")),
            "bar_range_pct": None,
            "variants": {},
        }
        if meta:
            rec["meta"] = meta

        # 대조군 — 세션 VWAP (현행 라이브 로직)
        sv = session_vwap_series(bars)
        base_res = touch_signal(bars, sv, j, self.vwap_tol,
                                self.bar_range_pct, self.stop_buf_pct)
        rec["bar_range_pct"] = base_res["bar_range"]
        rec["variants"]["session"] = self._pack(base_res, anchor=0)

        # 실험군 — 임계값별 AVWAP
        for m in self.mults:
            ai = anchor_index(bars, m, base_vol_per_bar)
            av = avwap_series(bars, ai)
            r = touch_signal(bars, av, j, self.vwap_tol,
                             self.bar_range_pct, self.stop_buf_pct)
            packed = self._pack(r, anchor=ai)
            if ai is not None:
                packed["anchor_ts"] = self._bar_ts(bars, ai)
                packed["anchor_bars_ago"] = j - ai
            rec["variants"][f"avwap_{m:g}x"] = packed

        self._write(day, rec)
        return rec

    @staticmethod
    def _pack(r, anchor):
        return {
            "anchor_idx": anchor,
            "vwap": _round(r["vwap"]),
            "trend": r["trend"],
            "touch": r["touch"],
            "reclaim": r["reclaim"],
            "raw_signal": r["raw_signal"],
            "bar_ok": r["bar_ok"],
            "signal": r["signal"],
            "gap_pct": _round(r["gap_pct"]),
            "entry": _round(r["entry"]),
            "stop": _round(r["stop"]),
        }

    @staticmethod
    def _bar_ts(bars, i):
        v = _f(bars[i], "ts")
        return str(v) if v is not None else None

    def _write(self, day, rec):
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, f"{day}.jsonl")
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


def _round(x, nd=4):
    return None if x is None else round(float(x), nd)
