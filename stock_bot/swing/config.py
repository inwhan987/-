# -*- coding: utf-8 -*-
"""스윙봇 설정 — `.env.swing` + `.env.overrides` + 환경변수.

시크릿은 여기 없다. KIS 키·계좌는 stock_bot.config.settings(.env) 가 갖는다.
우선순위: 환경변수 > .env.overrides(웹 파라미터 저장) > .env.swing. 값은 전부 문자열로
읽어 여기서 변환한다. 운영 스위치(SWING_TRADE_ENABLED)는 장중 핫리로드 — trade_enabled_now().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_SWING = ROOT / ".env.swing"
ENV_OVERRIDES = ROOT / ".env.overrides"


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.split("#", 1)[0].strip().strip('"').strip("'")   # 행 끝 주석 제거
        out[k.strip()] = v
    return out


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class SwingCfg:
    mode: str = "dryrun"
    trade_enabled: bool = True       # false = 신규매수만 차단 (청산·손절·익절은 계속)
    strategies: list[str] = field(default_factory=list)   # 빈 리스트 = ALL
    use_trend_filter: bool = False
    data_kis_env: str = "paper"

    watch_new: int = 30
    watch_hold: int = 6
    watch_mode: str = "even"
    bar_sec: int = 180
    bar_store_sec: int = 60

    regime_enabled: bool = True
    regime_index: str = "0001"
    regime_ma: int = 200
    regime_below_mult: float = 0.0

    entry_from: str = "093000"
    entry_until: str = "151500"

    entry_min_value_eok: float = 30.0
    entry_min_cap_eok: float = 1000.0
    entry_min_price: float = 1000.0
    entry_max_atr_pct: float = 0.15
    max_order_share: float = 0.01

    position_krw: float = 10_000_000
    max_positions: int = 5
    max_new_per_day: int = 2
    stop_pct: float = 0.20
    tp_pct: float = 0.12
    trail_after: float = 0.08
    trail_pct: float = 0.05
    time_stop_days: int = 20
    exit_trend_break: bool = False

    collect_program: bool = True     # 야간 프로그램매매 수집 (KIS 1/s 예산 절약용 토글)
    dart_enabled: bool = False       # 주간 DART 전종목 배치(scripts/swing_dart_weekly.py) on/off (기록용)
    dart_budget_sec: int = 600

    db_path: str = "data/swing.db"

    @property
    def strategy_names(self) -> list[str] | None:
        """None 이면 전부(bt_swing REGISTRY 전체)."""
        return self.strategies or None


def load(path: Path | None = None) -> SwingCfg:
    f = {**_read_env_file(path or ENV_SWING), **_read_env_file(ENV_OVERRIDES)}

    def g(key: str, default: str) -> str:
        return os.environ.get(key, f.get(key, default))

    mode = g("SWING_MODE", "dryrun").strip().lower()
    if mode not in ("dryrun", "paper", "live"):
        raise SystemExit(f"SWING_MODE 값이 잘못됨: {mode!r} (dryrun|paper|live)")
    strat_raw = g("SWING_STRATEGIES", "ALL").strip()
    strategies = [] if strat_raw.upper() in ("", "ALL") else \
        [s.strip().upper() for s in strat_raw.split(",") if s.strip()]
    data_env = g("SWING_DATA_KIS_ENV", "paper").strip().lower()
    if data_env not in ("paper", "real"):
        raise SystemExit(f"SWING_DATA_KIS_ENV 값이 잘못됨: {data_env!r}")
    wm = g("SWING_WATCH_MODE", "even").strip().lower()
    if wm not in ("even", "top"):
        raise SystemExit(f"SWING_WATCH_MODE 값이 잘못됨: {wm!r}")

    db = g("SWING_DB_PATH", "data/swing.db")
    if not os.path.isabs(db):
        db = str(ROOT / db)

    return SwingCfg(
        mode=mode, strategies=strategies,
        trade_enabled=_bool(g("SWING_TRADE_ENABLED", "true")),
        use_trend_filter=_bool(g("SWING_USE_TREND_FILTER", "false")),
        data_kis_env=data_env,
        watch_new=int(g("SWING_WATCH_NEW", "30")),
        watch_hold=int(g("SWING_WATCH_HOLD", "6")),
        watch_mode=wm,
        bar_sec=int(g("SWING_BAR_SEC", "180")),
        bar_store_sec=int(g("SWING_BAR_STORE_SEC", "60")),
        regime_enabled=_bool(g("SWING_REGIME_ENABLED", "true")),
        regime_index=g("SWING_REGIME_INDEX", "0001").strip(),
        regime_ma=int(g("SWING_REGIME_MA", "200")),
        regime_below_mult=float(g("SWING_REGIME_BELOW_MULT", "0.0")),
        entry_from=g("SWING_ENTRY_FROM", "093000").strip(),
        entry_until=g("SWING_ENTRY_UNTIL", "151500").strip(),
        entry_min_value_eok=float(g("SWING_ENTRY_MIN_VALUE_EOK", "30")),
        entry_min_cap_eok=float(g("SWING_ENTRY_MIN_CAP_EOK", "1000")),
        entry_min_price=float(g("SWING_ENTRY_MIN_PRICE", "1000")),
        entry_max_atr_pct=float(g("SWING_ENTRY_MAX_ATR_PCT", "0.15")),
        max_order_share=float(g("SWING_MAX_ORDER_SHARE", "0.01")),
        position_krw=float(g("SWING_POSITION_KRW", "10000000")),
        max_positions=int(g("SWING_MAX_POSITIONS", "5")),
        max_new_per_day=int(g("SWING_MAX_NEW_PER_DAY", "2")),
        stop_pct=float(g("SWING_STOP_PCT", "0.20")),
        tp_pct=float(g("SWING_TP_PCT", "0.12")),
        trail_after=float(g("SWING_TRAIL_AFTER", "0.08")),
        trail_pct=float(g("SWING_TRAIL_PCT", "0.05")),
        time_stop_days=int(g("SWING_TIME_STOP_DAYS", "20")),
        exit_trend_break=_bool(g("SWING_EXIT_TREND_BREAK", "false")),
        collect_program=_bool(g("SWING_COLLECT_PROGRAM", "true")),
        dart_enabled=_bool(g("SWING_DART_ENABLED", "false")),
        dart_budget_sec=int(g("SWING_DART_BUDGET_SEC", "600")),
        db_path=db,
    )


_CFG: SwingCfg | None = None


def cfg() -> SwingCfg:
    global _CFG
    if _CFG is None:
        _CFG = load()
    return _CFG


# ── 장중 핫리로드 스위치 ───────────────────────────────────────────────
# 도커는 env_file 값을 os.environ 에 고정하므로, 웹에서 .env.overrides 를 바꿔도 환경변수로는
# 안 보인다. 이 키만은 파일을 직접 읽어 파일 값이 환경변수보다 앞선다(스톡봇 _reload_env_if_changed 와 동일 원칙).
_OVR_CACHE: dict = {"mtime": None, "val": None}


def trade_enabled_now(default: bool = True) -> bool:
    """SWING_TRADE_ENABLED 현재값. .env.overrides 의 값 > 환경변수 > default. mtime 캐시."""
    try:
        m = ENV_OVERRIDES.stat().st_mtime if ENV_OVERRIDES.exists() else None
    except OSError:
        m = None
    if m != _OVR_CACHE["mtime"]:
        _OVR_CACHE["mtime"] = m
        _OVR_CACHE["val"] = _read_env_file(ENV_OVERRIDES).get("SWING_TRADE_ENABLED") if m else None
    v = _OVR_CACHE["val"]
    if v is None:
        v = os.environ.get("SWING_TRADE_ENABLED")
    return default if v is None else _bool(v)
