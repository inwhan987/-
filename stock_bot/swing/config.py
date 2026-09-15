# -*- coding: utf-8 -*-
"""스윙봇 설정 — 전역 settings(.env + .env.overrides) 의 SWING_* 를 dataclass 로 묶는다.

별도 .env.swing 은 없다(2026-09-16 폐지). 스톡봇·대장주와 같은 파일·같은 우선순위
(환경변수 > .env.overrides > .env > 코드 기본값) 를 쓰고, 값 정의는 stock_bot.config.settings.

실행 모드는 전역과 동기(SWING_MODE 없음):
  TRADE_DRY_RUN=true → dryrun(주문 없음) / 아니면 KIS_ENV paper → paper(모의), real → live(실전)

운영 스위치(SWING_TRADE_ENABLED)는 장중 핫리로드 — trade_enabled_now().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_MAIN = ROOT / ".env"
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


def mode_of(trade_dry_run: bool, kis_env: str) -> str:
    """전역 설정 → 스윙 실행 모드. 스톡봇·대장주와 같은 스위치로 갈린다."""
    if trade_dry_run:
        return "dryrun"
    return "paper" if str(kis_env).lower() == "paper" else "live"


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

    # 슬롯·1건 금액은 스톡봇 공용(STOCK_MAX_POSITIONS·STOCK_BUDGET_KRW) — shared_slots_now() 로 장중 핫리드.
    position_krw: float = 2_000_000     # 시작 시 스냅샷(폴백용). 실제 사이징은 shared_slots_now()[1]
    max_positions: int = 5              # 시작 시 스냅샷(폴백용). 실제 판정은 shared_slots_now()[0]
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


def load() -> SwingCfg:
    from stock_bot.config import settings as s

    strat_raw = str(s.swing_strategies or "ALL").strip()
    strategies = [] if strat_raw.upper() in ("", "ALL") else         [x.strip().upper() for x in strat_raw.split(",") if x.strip()]

    db = s.swing_db_path
    if not os.path.isabs(db):
        db = str(ROOT / db)

    return SwingCfg(
        mode=mode_of(s.trade_dry_run, s.kis_env),
        strategies=strategies,
        trade_enabled=bool(s.swing_trade_enabled),
        use_trend_filter=bool(s.swing_use_trend_filter),
        data_kis_env=s.swing_data_kis_env,
        watch_new=s.swing_watch_new,
        watch_hold=s.swing_watch_hold,
        watch_mode=s.swing_watch_mode,
        bar_sec=s.swing_bar_sec,
        bar_store_sec=s.swing_bar_store_sec,
        regime_enabled=s.swing_regime_enabled,
        regime_index=str(s.swing_regime_index).strip(),
        regime_ma=s.swing_regime_ma,
        regime_below_mult=s.swing_regime_below_mult,
        entry_from=str(s.swing_entry_from).strip(),
        entry_until=str(s.swing_entry_until).strip(),
        entry_min_value_eok=s.swing_entry_min_value_eok,
        entry_min_cap_eok=s.swing_entry_min_cap_eok,
        entry_min_price=s.swing_entry_min_price,
        entry_max_atr_pct=s.swing_entry_max_atr_pct,
        max_order_share=s.swing_max_order_share,
        position_krw=_slot_krw(s.stock_budget_krw, s.stock_max_positions, s.trade_cash_per_trade),
        max_positions=s.stock_max_positions,
        max_new_per_day=s.swing_max_new_per_day,
        stop_pct=s.swing_stop_pct,
        tp_pct=s.swing_tp_pct,
        trail_after=s.swing_trail_after,
        trail_pct=s.swing_trail_pct,
        time_stop_days=s.swing_time_stop_days,
        exit_trend_break=s.swing_exit_trend_break,
        collect_program=s.swing_collect_program,
        dart_enabled=s.swing_dart_enabled,
        dart_budget_sec=s.swing_dart_budget_sec,
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
# 안 보인다. 이 키만은 파일을 직접 읽어 파일 값이 환경변수보다 앞선다(스톡봇 _reload_env_if_changed 와 동일 원칙:
# .env.overrides > .env).
_OVR_CACHE: dict = {"mtime": None, "val": None}


def _slot_krw(budget: float, slots: int, fallback: float) -> float:
    """공용 슬롯 1건 금액 = STOCK_BUDGET_KRW / STOCK_MAX_POSITIONS (runner.slot_krw 와 같은 식)."""
    return budget / slots if budget > 0 and slots > 0 else float(fallback)


_SHARED_CACHE: dict = {"mtime": None, "val": None}


def shared_slots_now(default_slots: int, default_krw: float) -> tuple[int, float]:
    """공용 슬롯 (최대 종목 수, 1건 금액) 현재값. STOCK_MAX_POSITIONS·STOCK_BUDGET_KRW 를
    .env.overrides > .env > 환경변수 순으로 읽는다(mtime 캐시). 웹에서 바꾸면 재시작 없이 반영."""
    try:
        m = tuple(p.stat().st_mtime if p.exists() else None for p in (ENV_OVERRIDES, ENV_MAIN))
    except OSError:
        m = None
    if m != _SHARED_CACHE["mtime"] or _SHARED_CACHE["val"] is None:
        merged = {**_read_env_file(ENV_MAIN), **_read_env_file(ENV_OVERRIDES)}
        def _get(k, cast, dflt):
            v = merged.get(k)
            if v is None:
                v = os.environ.get(k)
            try:
                return cast(v) if v not in (None, "") else dflt
            except (TypeError, ValueError):
                return dflt
        slots = _get("STOCK_MAX_POSITIONS", int, default_slots)
        budget = _get("STOCK_BUDGET_KRW", float, 0.0)
        # 파일에 STOCK_BUDGET_KRW 가 없으면 시작 시 스냅샷(settings 기본값 반영)을 그대로 — runner 와 동일 금액 보장.
        _SHARED_CACHE["mtime"] = m
        _SHARED_CACHE["val"] = (slots, _slot_krw(budget, slots, default_krw))
    return _SHARED_CACHE["val"]


def trade_enabled_now(default: bool = True) -> bool:
    """SWING_TRADE_ENABLED 현재값. .env.overrides > .env > 환경변수 > default. mtime 캐시."""
    try:
        m = tuple(p.stat().st_mtime if p.exists() else None for p in (ENV_OVERRIDES, ENV_MAIN))
    except OSError:
        m = None
    if m != _OVR_CACHE["mtime"]:
        _OVR_CACHE["mtime"] = m
        merged = {**_read_env_file(ENV_MAIN), **_read_env_file(ENV_OVERRIDES)}
        _OVR_CACHE["val"] = merged.get("SWING_TRADE_ENABLED")
    v = _OVR_CACHE["val"]
    if v is None:
        v = os.environ.get("SWING_TRADE_ENABLED")
    return default if v is None else _bool(v)
