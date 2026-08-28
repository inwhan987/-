"""대장주 눌림목 전략 실전 모듈 (backtest_leader_pullback.py 의 라이브 구현).

흐름 (백테스트 06-09~06-12 확정 설정과 동일):
  · 대상   : 당일 data/leader_picks/날짜.json 의 1등 섹터 top3 바스켓
             — 바스켓 60%룰: 2·3등 stock_score ≥ 1등 × leader_band_ratio 일 때만 편입
             — 기존 앙상블 전략 종목(settings.symbols)과 겹치면 제외 (자본·포지션 충돌 방지)
  · 고점   : 9:00~선별시각 최고가(pre_high), floor = pre_high × (1 - 눌림한도)
  · 진입   : leader_interval_min 분봉에서 W=leader_w 스윙저점 확정봉 → 시장가 매수
             — 마지막 확정봉만 평가 (재시작 후 스테일 신호 추격 방지)
             — 같은 봉 동시 신호 시 순위 우선(1등>2등>3등)
             — leader_max_positions(기본 1) 슬롯만큼 동시진입 가능, 같은 코드 재진입은 없음
             — 붕괴컷: 전고점 이후 floor 를 깬 종목은 보류
             — 상한가컷: 목표가 > 전일종가 × 1.30 이면 스킵
  · 손절   : 스윙저점 × (1 - leader_stop_buf_pct/100)
  · 익절   : 진입가 × (1 + leader_tp_pct/100)
  · 마감   : leader_close_time(기본 14:55) 시장가 청산

상태는 data/leader_trade_state/날짜.json 에 영속 — 컨테이너 재시작에도 보유
포지션·완료 여부가 유지된다. runner 가 평일 장중 매분 tick() 을 호출한다.

관전 모드 (2026-07-16): LEADER_TRADE_ENABLED=off 여도 분봉 조회·차트 스냅샷·
신호 판정은 그대로 수행하고 진입/청산만 가상으로 처리한다(실주문·record_trade·
점유원장 없음, 상태에 virtual=True). 회복확인·장대양봉컷으로 스윙저점을
버린 것도 로그에 남겨 전략이 정상 동작하는지 눈으로 검증할 수 있게 한다.
장중 핫리로드로 off→on 전환 시 가상 보유분은 가상으로만 청산된다.
"""
from __future__ import annotations

import json
import threading
import time as _time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

from loguru import logger

from stock_bot.broker import KISBroker
from stock_bot.broker.kis import OrderRejectedError
from stock_bot.config import settings
from stock_bot.live import chart_snapshot
from stock_bot.live import position_owner
from stock_bot.live.avwap_probe import AvwapProbe
from stock_bot.market_calendar import KST as _KST
from stock_bot.lead_score import to_display_sector, to_display_stock
from stock_bot.names import get_name
from stock_bot.notify import notify
from stock_bot.storage import record_trade

# 같은 매도 거부 메시지를 이 초 안에 다시 알리지 않는다(디스코드 도배 방지).
# 로그는 매번 남으므로 추적성은 유지된다.
_REJECT_NOTIFY_SEC = 600

# 백테스트와 동일 수수료 (검증·로그용 net 계산)
_BUY_COMM = 0.00015
_SELL_COMM = 0.00195

_ROOT = Path(__file__).resolve().parents[2]
_PICKS_DIR = _ROOT / "data" / "leader_picks"
_SLOPE_BARS = 5   # VWAP 기울기 측정 구간(확정봉 수). 3분봉 기준 15분.
_STATE_DIR = _ROOT / "data" / "leader_trade_state"


def _parse_hm(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = text.strip().split(":")[:2]
        return (int(hh), int(mm))
    except Exception:
        return default


def _bare(code: str) -> str:
    return code.split(".")[0].strip()


def _dynamic_fib(strength_pct: float) -> float:
    """아침 임펄스다리 상승강도(%) → 피보 되돌림 계수 (관측용·leader_fib_dynamic).

    약한 임펄스는 깊은 눌림을 사지 않고 고정 얕은 눌림만, 강할수록 되돌림 허용.
    NOTE: 2026-08-02 4일 표본(7/28~31) 백테스트에선 다리강도가 승패를 갈라내지
    못했음 — '검증된 엣지'가 아니라 관전 모드에서 동작을 눈으로 보기 위한 관측 장치.
    """
    if strength_pct < 10.0:
        return 0.0      # 고정 pull 유지(보수)
    if strength_pct < 15.0:
        return 0.382
    return 0.5


# 정식 틱이 exit_fast 의 락 해제를 기다리는 최대 시간(초).
# exit_fast 1회 = 보유 종목 수만큼 KIS 시세 조회. 한가할 땐 1초 내외지만
# 모의계좌 유량 한도(초당 1건)를 두 봇이 공유하므로 매분 :00 메인봇 버스트와
# 겹치면 그 1건이 수 초씩 밀린다 → 3초로는 부족해 틱이 통째로 스킵됐다
# (2026-08-19 실측: 137분 중 30회 스킵, 그중 1/3이 3분봉 확정 시각).
_TICK_LOCK_WAIT_SEC = 12.0

# exit_fast 가 분 경계를 정식 틱에 양보하는 구간(초). 이 창 안에서는 fast 패스를
# 건너뛰어 :00 에 발화하는 tick 이 락을 즉시 잡게 한다. 청산 감시가 늦어지는
# 최대치는 fast 주기 1회(5초)라 사실상 손실이 없고, 대신 3분봉 확정 스캔을
# 잃지 않는다.
_FAST_YIELD_TAIL_SEC = 55   # 이 초 이상이면 다음 분 틱에 양보
_FAST_YIELD_HEAD_SEC = 3    # 이 초 이하면 진행 중인 틱에 양보


class LeaderTrader:
    """하루 단위 슬롯 관리자: 종목별 watching → holding → done (슬롯 수=leader_max_positions)."""

    def __init__(self, broker: KISBroker) -> None:
        self.broker = broker
        self._date = ""            # 상태가 로드된 날짜 (YYYY-MM-DD)
        self._state: dict[str, Any] = {}
        self._basket: list[dict[str, Any]] = []
        self._switch_watch: list[dict[str, Any]] = []  # 전환 감시용 상위섹터 1등(차트+판정)
        self._sector_baskets: dict[str, list[dict[str, Any]]] = {}   # 섹터명 → basket
        self._sector_start_times: dict[str, tuple[int, int]] = {}    # 섹터명 → (h, m)
        self._chart_only_sectors: dict[str, list[dict[str, Any]]] = {}  # §4-4 축출됐지만 차트만 유지(진입 감시 종료)
        self._active_sector_name: str = ""             # 현재 매매 중인 섹터명(전환 추적)
        self._trade_start: tuple[int, int] = (9, 30)
        self._no_picks_logged = ""  # picks 미존재 로그 1회 제한용 날짜
        self._soft_logged = ""      # 상한가컷 로그 중복 방지 (code:bar_time)
        self._near_logged: set[str] = set()  # 근접신호(회복확인 등 미충족) 로그 중복 방지
        self._watch_logged: set[str] = set()  # 관망(스윙저점 미형성) 로그 중복 방지 (code:bar_time)
        self._sell_reject: dict[str, tuple[str, float]] = {}  # 코드 → (거부 알림문, ts)
        self._lock = threading.Lock()  # tick()/check_exit_fast() 동시 실행(스레드풀) 방지
        self._avwap_probe = AvwapProbe()  # AVWAP 섀도 로깅(§3단계) — 매매 로직 무간섭 관찰자

    # ── 표시 헬퍼 ────────────────────────────────────────────────────
    def _disp(self, code: str) -> str:
        """로그·알림용 '코드 종목명' 문자열. 바스켓·감시세트 → get_name 폴백."""
        bc = _bare(code)
        for m in self._basket:
            if _bare(m.get("code", "")) == bc and m.get("name"):
                return f"{bc} {m['name']}"
        nm = get_name(bc)
        return f"{bc} {nm}" if nm and nm != bc else bc

    # ── 상태 영속 ────────────────────────────────────────────────────
    def _state_path(self, date: str) -> Path:
        return _STATE_DIR / f"{date}.json"

    @staticmethod
    def _read_leaders(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """picks JSON 파싱 → (leaders 리스트, 전체 payload). 실패 시 ([], {})."""
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
            return (p.get("leaders") or []), p
        except Exception:
            return [], {}

    @staticmethod
    def _top3_of(lead: dict[str, Any]) -> list[dict[str, Any]]:
        top3 = lead.get("top3") or []
        if not top3:  # top3 기록 이전 포맷 → 1등만
            top3 = [{"rank": 1, "code": lead["code"],
                     "name": lead.get("name", ""),
                     "change_pct": lead.get("change_pct", 0)}]
        return sorted(top3, key=lambda x: x.get("rank", 9))

    def _build_basket(self, top3: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """바스켓 룰 + own-symbol 제외 적용해 진입 바스켓 구성.

        60%룰 통일: 1등 stock_score 대비 leader_band_ratio(=0.6) 이상만 편입.
        점수 부재 시(구 포맷) 보수적으로 1등만.
        """
        if not top3:
            return []
        lead_sc = float(top3[0].get("stock_score", 0) or 0)
        if lead_sc > 0:
            ratio = settings.leader_band_ratio
            basket = [
                m for m in top3
                if float(m.get("stock_score", 0) or 0) >= lead_sc * ratio
            ]
        else:
            basket = [top3[0]]
        # 기존 전략 종목과 겹치면 제외 — 단 own-symbol 우선권이 켜지면 점유락으로
        # 상호배제하므로 제외하지 않는다(스톡봇이 안 잡은 종목을 대장주가 잡을 수 있게).
        if not settings.leader_own_symbol_priority:
            own = {_bare(s) for s in settings.symbols}
            basket = [m for m in basket if _bare(m["code"]) not in own]
        return basket

    def _collect_switch_watch(self, leaders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """전환 감시용 상위 K 섹터의 1등(대장) 목록 — 차트 스냅샷+전환 후보."""
        if not settings.leader_switch_enabled:
            return []
        k = max(1, settings.leader_max_sectors)
        return [{"code": L["code"], "name": L.get("name", ""), "sector": L.get("sector", "")}
                for L in leaders[:k]]

    def _flatten_baskets(self) -> list[dict[str, Any]]:
        """sector_baskets 전체를 합쳐 중복 제거한 단일 basket 반환.

        순서 = 감시 섹터 순서(재정렬 후엔 sector_score 순) → 섹터 내 rank 순.
        _scan_entries 가 이 순서로 훑으며 '동시 신호 시 순위 우선'을 적용한다.
        """
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for basket in self._sector_baskets.values():
            for m in basket:
                c = _bare(m["code"])
                if c not in seen:
                    seen.add(c)
                    result.append(m)
        return result

    def _load_day(self, date: str) -> None:
        """날짜가 바뀌면 picks·상태 재로드. 전환으로 reval 섹터를 잡았으면 그 스냅샷 기준."""
        self._date = date
        self._basket = []
        self._switch_watch = []
        self._sector_baskets = {}
        self._sector_start_times = {}
        self._chart_only_sectors = {}
        self._active_sector_name = ""
        self._near_logged = set()
        self._watch_logged = set()
        path = self._state_path(date)
        try:
            self._state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._state = {"status": "watching"}

        self._migrate_legacy_state()
        self._state.setdefault("positions", {})

        picks_path = _PICKS_DIR / f"{date}.json"
        if not picks_path.exists():
            return
        leaders, meta = self._read_leaders(picks_path)
        if not leaders:
            logger.warning("leader_trader: picks 파싱/빈값 {}", picks_path.name)
            return
        self._trade_start = _parse_hm(str(meta.get("selected_at", "09:30")), (9, 30))
        # 오늘 전환으로 기준시각이 갱신됐었다면(재시작 복구) 그 값을 유지.
        _ts_sw = self._state.get("trade_start_switch")
        if _ts_sw:
            self._trade_start = _parse_hm(str(_ts_sw), self._trade_start)

        # 활성 섹터 결정: 전환으로 reval 을 잡았으면(active_source=reval) 그 스냅샷에서
        # 저장된 섹터명으로, 아니면 정본 1등. 전환 OFF·미발생 시 정본 leaders[0] 과 동일.
        active_leaders = leaders
        if self._state.get("active_source") == "reval":
            rev, _ = self._read_leaders(_PICKS_DIR / f"{date}_reval.json")
            if rev:
                active_leaders = rev
        idx = 0
        name = self._state.get("active_sector_name")
        if name:
            idx = next((k for k, L in enumerate(active_leaders)
                        if L.get("sector") == name), 0)
        lead = active_leaders[idx]
        self._active_sector_name = lead.get("sector", "")
        self._state.setdefault("active_sector_name", self._active_sector_name)
        self._state.setdefault("active_chg", float(lead.get("change_pct", 0)))

        top3 = self._top3_of(lead)
        initial_basket = self._build_basket(top3)
        # 멀티섹터 바스켓 초기화
        if initial_basket:
            self._sector_baskets[self._active_sector_name] = initial_basket
            self._sector_start_times[self._active_sector_name] = self._trade_start
        # 재시작 복구: 이전 세션에서 감시 중이던 추가 섹터들 복원
        saved_sectors = self._state.get("watched_sectors", [])
        # §4-2 첫 선별 다중섹터: 최초 로드(재시작·전환 복구 아님)일 때,
        # 정본 leaders 중 1등 sector_score 대비 leader_band_ratio(=0.6) 이상 섹터를
        # 최대 leader_max_sectors 개까지 함께 시딩.
        if (not saved_sectors
                and self._state.get("active_source") != "reval" and initial_basket):
            top_score = float(active_leaders[idx].get("sector_score", 0) or 0)
            if top_score > 0:
                ratio = settings.leader_band_ratio
                maxs = max(1, settings.leader_max_sectors)
                for L in active_leaders:
                    if len(self._sector_baskets) >= maxs:
                        break
                    s_name = L.get("sector", "")
                    if not s_name or s_name in self._sector_baskets:
                        continue
                    if float(L.get("sector_score", 0) or 0) >= top_score * ratio:
                        extra = self._build_basket(self._top3_of(L))
                        if extra:
                            self._sector_baskets[s_name] = extra
                            self._sector_start_times[s_name] = self._trade_start
                if len(self._sector_baskets) > 1:
                    self._state["watched_sectors"] = list(self._sector_baskets.keys())
                    self._state["sector_starts"] = {
                        s: f"{t[0]:02d}:{t[1]:02d}"
                        for s, t in self._sector_start_times.items()
                    }
                    self._save_state()
                    saved_sectors = self._state["watched_sectors"]
        saved_starts = self._state.get("sector_starts", {})
        if len(saved_sectors) > 1:
            reval_path = _PICKS_DIR / f"{date}_reval.json"
            rev_restore, _ = self._read_leaders(reval_path)
            # 전환(reval) 섹터 우선, 정본 leaders 로 폴백(첫선별 다중섹터 복원용).
            by_sector = {L.get("sector", ""): L for L in rev_restore}
            for L in leaders:
                by_sector.setdefault(L.get("sector", ""), L)
            if by_sector:
                for s_name in saved_sectors:
                    if s_name in self._sector_baskets:
                        continue
                    s_leader = by_sector.get(s_name)
                    if s_leader:
                        extra = self._build_basket(self._top3_of(s_leader))
                        if extra:
                            self._sector_baskets[s_name] = extra
                            start_str = saved_starts.get(s_name)
                            self._sector_start_times[s_name] = (
                                _parse_hm(start_str, self._trade_start) if start_str else self._trade_start
                            )
        # §4-4 차트전용 섹터 복원: 진입 감시는 안 하되 차트만 유지.
        co_names = self._state.get("chart_only_sectors", []) or []
        if co_names:
            reval_path = _PICKS_DIR / f"{date}_reval.json"
            rev_restore, _ = self._read_leaders(reval_path)
            by_sector = {L.get("sector", ""): L for L in rev_restore}
            for L in leaders:
                by_sector.setdefault(L.get("sector", ""), L)
            for s_name in co_names:
                if s_name in self._sector_baskets or s_name in self._chart_only_sectors:
                    continue
                s_leader = by_sector.get(s_name)
                if s_leader:
                    cb = self._build_basket(self._top3_of(s_leader))
                    if cb:
                        self._chart_only_sectors[s_name] = cb
        self._basket = self._flatten_baskets()
        self._switch_watch = []  # 모든 감시섹터가 _basket에 포함되므로 불필요
        if self._basket and not self._state.get("positions"):
            lead_sc = float(top3[0].get("stock_score", 0) or 0)
            thresh_sc = lead_sc * settings.leader_band_ratio
            logger.info(
                "leader_trader: {} 바스켓 {} [섹터 {}] (선별 {:02d}:{:02d}, {:.0f}%룰 종목점수 기준 {:.1f}점 / raw {:.3f})",
                date,
                ", ".join(f"{m.get('name', '')}({m['code']})" for m in self._basket),
                self._active_sector_name or "1등",
                *self._trade_start, settings.leader_band_ratio * 100,
                to_display_stock(thresh_sc), thresh_sc,
            )
            # 2026-08-24: meta["criteria"] 재출력 제거.
            # leader_finder 가 선별 시점에 같은 내용을 이미 같은 로그파일에
            # 찍고, 트레이더가 3분 뒤 바스켓을 읽으면서 또 통째로 복사해
            # 로그가 두 번씩 겹쳐 보였다. 원본은 선별 블록에 그대로 남아있다.

    def _migrate_legacy_state(self) -> None:
        """구 스키마(단일 flat 상태) → positions 딕셔너리로 1회 변환.

        leader_max_positions 도입(2026-08-15) 전에 저장된 오늘자 상태 파일은
        symbol/qty/entry 등이 최상위에 그대로 있다("positions" 키 없음). 그대로
        두면 배포 직후 재로드 시 실제 보유 중인 포지션을 놓쳐(추적 유실) 실주문
        청산이 안 되는 사고로 이어질 수 있어, 감지되면 즉시 변환 후 저장한다.
        """
        if "positions" in self._state or "symbol" not in self._state:
            return
        code = _bare(self._state["symbol"])
        legacy_fields = (
            "symbol", "name", "rank", "qty", "entry", "ref", "stop", "tp",
            "entry_at", "bar_time", "src", "peak", "virtual",
            "exit", "exit_at", "exit_reason", "net_pct", "split_done",
        )
        pos = {k: self._state[k] for k in legacy_fields if k in self._state}
        pos["status"] = self._state.get("status", "watching")
        self._state["positions"] = {code: pos}
        for k in legacy_fields:
            self._state.pop(k, None)
        logger.warning(
            "leader_trader: 구 상태 스키마 감지 — {} 포지션을 positions 로 마이그레이션",
            self._disp(code),
        )
        self._save_state()

    def _sync_status(self) -> None:
        """요약 status 를 positions 로부터 재파생한다.

        status 는 positions 에서 나오는 파생값인데 별도 필드로 영속돼 어긋나기
        쉬웠다. 실제로 _enter() 는 positions 만 쓰고 저장해 파일이
        status="watching" + positions{holding} 인 모순 상태가 됐고, 이걸 읽는
        leader_runner 의 재선별 게이트가 뚫려 보유 중에도 순위계산이 계속 돌았다
        (2026-08-18). 저장 직전 항상 재계산해 어느 경로로 저장하든 어긋날 수
        없게 한다."""
        positions = self._state.setdefault("positions", {})
        if any(p.get("status") == "holding" for p in positions.values()):
            self._state["status"] = "holding"
        elif len(positions) >= max(1, settings.leader_max_positions):
            self._state["status"] = "done"
        else:
            self._state["status"] = "watching"

    def _snapshot_baskets(self) -> None:
        """현재 섹터별 감시 바스켓을 상태파일에 기록(관측 전용, 저장만).

        지금까지 상태파일에는 섹터 '이름'만 남아서(watched_sectors) 웹이 보여주는
        60%룰 재계산 결과와 트레이더가 실제로 감시 중인 종목이 다를 때 사후에
        확인할 방법이 없었다. 여기서 실제 바스켓을 그대로 덤프해 둔다.

        ※ 저장만 한다 — 재시작 시 이 값을 복원하지 않는다. 복원은 매매 로직
          변경(현행: 재시작하면 picks 에서 다시 구성)이라 별도 승인이 필요하다.
        """
        try:
            self._state["sector_baskets"] = {
                s: [{"code": _bare(m.get("code", "")),
                     "name": m.get("name", ""),
                     "rank": m.get("rank", i + 1),
                     "change_pct": float(m.get("change_pct", 0) or 0),
                     "stock_score": m.get("stock_score", 0)}
                    for i, m in enumerate(basket)]
                for s, basket in self._sector_baskets.items()
            }
        except Exception:
            pass

    def _save_state(self) -> None:
        self._sync_status()
        self._snapshot_baskets()
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            self._state_path(self._date).write_text(
                json.dumps(self._state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("leader_trader: 상태 저장 실패 — {}", e)

    # ── 섹터 누적 추가 (관전 실험) ──────────────────────────────────
    def _maybe_switch(self, now: datetime) -> None:
        """장중 재선별(reval.json) 결과로 강한 신섹터를 감시 바스켓에 누적 추가한다.

        기존 섹터를 지우지 않고 유지하며 신섹터를 추가(최대 leader_max_sectors개).
        슬롯이 가득 찼을 때만 최하위 섹터를 퇴출 후 신섹터 편입.
        슬롯 소진 여부와 무관하게 호출 — 만석이어도 순위 변동을 계속 반영한다.

        예전엔 자체 5분 타이머(last_switch_eval)로 따로 게이트했는데, 그 타이머가
        leader_runner 의 reval 서브프로세스 완료 시점과 위상이 안 맞아 "재선별
        로그와 섹터 재정렬 로그 시각이 서로 다르다"는 혼동이 있었다. reval.json 의
        selected_at 스탬프가 바뀌었는지로 게이트하도록 바꿔 — reval 서브프로세스가
        새 결과를 쓰면 다음 1분 tick 에서 곧바로 반영된다(중복 처리는 없음).
        """
        try:
            uh, um = (int(x) for x in settings.leader_switch_until.split(":")[:2])
        except Exception:
            uh, um = 13, 0
        if (now.hour, now.minute) > (uh, um):
            return
        reval_path = _PICKS_DIR / f"{self._date}_reval.json"
        rev, meta = self._read_leaders(reval_path)
        if not rev:
            return
        stamp = meta.get("selected_at") or ""
        if stamp and stamp == self._state.get("last_switch_stamp"):
            return
        self._state["last_switch_eval"] = now.isoformat()
        self._state["last_switch_stamp"] = stamp

        # §4-3 통합 재정렬 — 보유+신규 통합 점수정렬 → 상위 max_sectors 유지.
        self._reval_resort(rev, now)

    def _reval_resort(self, rev: list[dict[str, Any]], now: datetime) -> None:
        """§4-3 통합 재정렬: 보유 섹터 + 신규 섹터를 sector_score 순 정렬 후
        상위 leader_max_sectors 개만 감시로 유지.

        · 보유 섹터 점수는 reval 최신값으로 재계산(reval 에 없으면 0 → 탈락 후보).
        · 신규는 1등 sector_score 대비 leader_band_ratio(=0.6) 이상만 후보.
        · 상위 밖으로 밀린 보유 섹터 → 차트전용(_chart_only_sectors)으로 이동.
        결과가 바뀔 때만 상태 저장·알림.
        """
        max_sectors = max(1, settings.leader_max_sectors)
        ratio = settings.leader_band_ratio
        rev_by_sector = {L.get("sector", ""): L for L in rev if L.get("sector")}
        rev_scores = {s: float(L.get("sector_score", 0) or 0) for s, L in rev_by_sector.items()}
        top_score = rev_scores.get(rev[0].get("sector", ""), 0.0)

        # 후보 통합: 보유 섹터(최신 점수) + 밴드 이상 신규 섹터.
        combined: dict[str, float] = {s: rev_scores.get(s, 0.0) for s in self._sector_baskets}
        for s, sc in rev_scores.items():
            if s not in combined and sc >= top_score * ratio:
                combined[s] = sc
        if not combined:
            return
        ranked = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)
        keep = [s for s, _ in ranked[:max_sectors]]
        keep_set = set(keep)

        # reval 상위 후보인데 탈락한 섹터 — 조용히 넘어가면 "재선별엔 나오는데
        # 왜 추가가 안 되냐"는 착시가 생겨 사유를 남긴다(밴드미달/슬롯초과).
        for L in rev[:max_sectors]:
            s = L.get("sector", "")
            if not s or s in keep_set:
                continue
            sc = rev_scores.get(s, 0.0)
            if s not in combined:
                reason = (f"밴드미달 (섹터점수 {to_display_sector(sc):.1f}점 "
                          f"< 1등×{ratio:.0%} {to_display_sector(top_score * ratio):.1f}점)")
            else:
                reason = f"슬롯초과 (감시 {max_sectors}개 이미 상위 점수로 유지 중)"
            logger.info("leader_trader: 섹터 미추가 — {} · {}", s, reason)

        before = set(self._sector_baskets)
        # 탈락(보유였는데 상위 밖) → 차트전용 이동.
        for s in list(self._sector_baskets):
            if s not in keep_set:
                self._chart_only_sectors[s] = self._sector_baskets.pop(s)
                self._sector_start_times.pop(s, None)
        # 신규/재진입(상위인데 미보유) → 바스켓 생성(차트전용에 있던 것도 승격).
        # 보유 섹터 → 종목 누적 추가(2026-08-20). 섹터는 5분마다 최신 점수로
        # 재정렬하면서 섹터 '안' 종목만 편입 시점에 얼어붙어 있던 비일관 해소.
        #   · 추가만 한다(A안=완전 교체 기각). 종목점수는 상승률 45% 비중이라
        #     눌리면 점수가 빠진다 — 완전 교체는 우리가 사려는 '눌린 상태'의
        #     종목을 바로 그 순간 감시에서 빼버려 전략과 방향이 반대다.
        #   · 힘 빠져 남는 종목은 신호(VWAP 첫눌림)가 안 나와 그냥 감시만 하다
        #     끝나므로 손실이 제한적이다(비대칭: 안 사면 그만 vs 살 걸 못 삼).
        for s in keep:
            if s in self._sector_baskets:
                L = rev_by_sector.get(s)
                if not L:
                    continue
                cur = self._sector_baskets[s]
                have = {_bare(m.get("code", "")) for m in cur}
                fresh = [m for m in self._build_basket(self._top3_of(L))
                         if _bare(m.get("code", "")) not in have]
                if fresh:
                    cur.extend(fresh)
                    cur.sort(key=lambda m: float(m.get("stock_score", 0) or 0),
                             reverse=True)
                    logger.info(
                        "leader_trader: {} 종목 추가 — {} (감시 {}종목)",
                        s, ",".join(m.get("name", "") for m in fresh), len(cur))
                continue
            L = rev_by_sector.get(s)
            if not L:
                continue
            nb = self._build_basket(self._top3_of(L))
            if nb:
                self._sector_baskets[s] = nb
                self._sector_start_times[s] = (now.hour, now.minute)
                self._chart_only_sectors.pop(s, None)

        # 감시 섹터를 최신 점수순(keep)으로 재정렬 — _flatten_baskets 는 dict 삽입
        # 순서를 그대로 쓰므로, 여기서 맞춰두지 않으면 진입 스캔 우선순위가
        # '점수순'이 아니라 '편입 시간순'으로 굳는다(나중에 추가된 섹터가 점수를
        # 앞질러도 뒤에 남아, 동시 신호 시 낮은 점수가 슬롯을 선점).
        self._sector_baskets = {
            s: self._sector_baskets[s] for s in keep if s in self._sector_baskets
        }

        after = set(self._sector_baskets)
        self._basket = self._flatten_baskets()
        if after == before:
            self._save_state()  # last_switch_eval 만 갱신
            return

        # 활성 섹터명은 최상위 유지 섹터로.
        self._active_sector_name = keep[0] if keep else self._active_sector_name
        self._state["active_source"] = "reval"
        self._state["active_sector_name"] = self._active_sector_name
        self._state["watched_sectors"] = list(self._sector_baskets.keys())
        self._state["sector_starts"] = {
            s: f"{t[0]:02d}:{t[1]:02d}" for s, t in self._sector_start_times.items()
        }
        self._state["chart_only_sectors"] = list(self._chart_only_sectors.keys())
        self._near_logged = set()
        self._watch_logged = set()
        self._save_state()

        added = after - before
        dropped = before - after
        sector_list = " + ".join(self._sector_baskets.keys())
        chg = []
        if added:
            chg.append("추가 " + ",".join(added))
        if dropped:
            chg.append("차트전용 " + ",".join(dropped))
        logger.info(
            "leader_trader: 🔄 섹터 재정렬({}) · 감시 [{}] · 바스켓 {}",
            " / ".join(chg) or "변동", sector_list,
            ", ".join(f"{m.get('name', '')}({m['code']})" for m in self._basket),
        )
        try:
            notify(
                f"🔄 **대장주 섹터 재정렬** ({' / '.join(chg) or '변동'})\n"
                f"감시섹터: {sector_list}\n"
                f"바스켓: " + ", ".join(f"{m.get('name', '')}({m['code']})" for m in self._basket)
            )
        except Exception:
            pass

    # ── 메인 틱 ──────────────────────────────────────────────────────
    def tick(self) -> None:
        """매분 정식 틱 — entry 스캔(3분봉 확정) + 보유 관리. check_exit_fast()
        와 동시 실행되면 상태 레이스가 나므로 락으로 상호 배제한다.

        exit_fast(5초 주기)는 매분 :00 에도 발화하므로 정식 틱과 정면충돌한다.
        예전엔 즉시 포기(blocking=False)해 3분봉 확정 시각의 진입 스캔이 통째로
        1분 밀렸다 — 눌림목 진입에서 1분은 크다. 이제 잠깐 기다렸다 잡는다.
        exit_fast 는 보유 종목 시세 조회뿐이라 보통 1초 안에 끝나고, 이 잡은
        max_instances=1 이라 다음 분 틱과 겹칠 위험도 없다."""
        if not self._lock.acquire(timeout=_TICK_LOCK_WAIT_SEC):
            # 여기까지 오면 exit_fast 가 비정상적으로 오래 잡고 있다는 뜻 → WARNING.
            logger.warning(
                "leader_trader: tick 스킵 — check_exit_fast 가 {:.0f}초 넘게 락 점유",
                _TICK_LOCK_WAIT_SEC)
            return
        try:
            self._tick_impl()
        finally:
            self._lock.release()

    def check_exit_fast(self) -> None:
        """보유 포지션 손절/익절 전용 초단위 체크 — 정식 tick(1분)보다 촘촘히
        돌려 청산 지연을 줄인다(2026-08-15). entry 스캔(3분봉 확정 기반)은
        건드리지 않고 holding 상태 포지션들만 _manage_position 으로 관리한다."""
        # 분 경계는 정식 틱에 양보 — fast 가 락을 쥔 채 유량 게이트에서 대기하면
        # :00 에 뜬 tick 이 3분봉 확정 스캔을 통째로 놓친다(2026-08-19).
        _sec = datetime.now(tz=_KST).second
        if _sec >= _FAST_YIELD_TAIL_SEC or _sec <= _FAST_YIELD_HEAD_SEC:
            return
        if not self._lock.acquire(blocking=False):
            return  # 정식 tick 이 진행 중 — 다음 fast 주기에 재시도
        try:
            now = datetime.now(tz=_KST)
            positions = self._state.get("positions", {})
            holding_codes = [c for c, p in positions.items() if p.get("status") == "holding"]
            for code in holding_codes:
                self._manage_position(code, now)
        finally:
            self._lock.release()

    def _tick_impl(self) -> None:
        # 매매 off 여도 리턴하지 않는다 — 관전 모드로 신호 판정·가상매매까지 수행
        now = datetime.now(tz=_KST)
        date = f"{now:%Y-%m-%d}"
        if date != self._date:
            self._load_day(date)

        positions = self._state.setdefault("positions", {})
        holding_codes = [c for c, p in positions.items() if p.get("status") == "holding"]
        max_pos = max(1, settings.leader_max_positions)
        slots_open = len(positions) < max_pos
        # 표시용 요약 상태(대시보드 하위호환) — leader_max_positions=1 이면 종전과 동일값.
        self._sync_status()

        # 점유 원장 정합 — 보유 종목은 confirmed, 청산·스테일 점유는 청소.
        # 가상 보유(관전)는 실제 점유가 아니므로 원장에 올리지 않는다.
        if settings.leader_own_symbol_priority:
            held = [c for c in holding_codes if not positions[c].get("virtual")]
            position_owner.reconcile("leader", held)

        # 보유·슬롯마감 상태에선 _scan_entries 가 안 돌아 차트 스냅샷이 멈출 수 있다 →
        # 여기서 바스켓+보유 종목 차트를 계속 떨궈 차트 탭이 얼지 않게 한다(표시 전용).
        if holding_codes or not slots_open:
            self._refresh_charts()

        # 보유 포지션은 슬롯 여유와 무관하게 항상 관리(청산 판정) — 슬롯이 남아있으면
        # 이 포지션은 그대로 두고 아래에서 섹터 전환·신규 진입 스캔을 계속한다.
        for code in holding_codes:
            self._manage_position(code, now)

        # picks 로드는 슬롯 여유와 무관 — 만석이어도 섹터 재정렬에 필요하다.
        if not (_PICKS_DIR / f"{date}.json").exists():
            if self._no_picks_logged != date and now.time() >= dtime(9, 40):
                logger.info("leader_trader: {} picks 미생성 — 선별 대기", date)
                self._no_picks_logged = date
            return
        if not self._basket:
            self._load_day(date)  # picks 가 틱 사이에 생성된 경우 재로드
            if not self._basket:
                return

        # 섹터 재정렬(관전 실험) — 만석일 때도 돈다.
        # 2026-08-27: 예전엔 이 호출이 slots_open 게이트 아래에 있어, 첫 매수 순간
        # 순위계산(_reval_resort)과 종목추가가 통째로 멈췄다. positions 는 청산분도
        # 세므로 leader_max_positions=1 이면 장 끝까지 죽은 상태였다 — 웹 섹터 순위가
        # 진입 시각에 얼어붙던 원인. leader_runner 는 2026-08-24 에 같은 만석 게이트를
        # 이미 풀었는데(leader_runner.py:489) 결과를 읽는 이쪽이 막혀 있어, _reval.json
        # 만 5분마다 새로 쓰이고 아무도 읽지 않았다.
        # 매매 비간섭은 성립한다 — 만석이면 아래 _scan_entries 가 도달 불가다.
        if settings.leader_switch_enabled:
            self._maybe_switch(now)

        if not slots_open:  # 오늘 사용 가능한 슬롯을 모두 소진 — 신규 진입 스캔 종료
            return

        # watching (슬롯 여유 있음 — 보유 종목이 있어도 나머지 슬롯으로 계속 진입 탐색)
        close_t = _parse_hm(settings.leader_close_time, (14, 55))
        if (now.hour, now.minute) >= close_t:
            # 마감 직전엔 신규 진입은 멈추되(스캔 종료), 차트 탭은 장 마감(15:30)
            # 까지 계속 갱신해 관전 화면이 14:55 에 얼지 않게 한다(표시 전용).
            self._refresh_charts()
            return
        self._scan_entries(now)

    # ── 차트 스냅샷 유지 ─────────────────────────────────────────────
    def _refresh_charts(self) -> None:
        """스캔이 안 도는 구간(보유/완료, 그리고 마감임박 관망)에서도 바스켓·보유
        종목 분봉 스냅샷을 계속 기록한다.

        표시 전용 — 매매 판정과 무관하며 실패는 조용히 무시한다. 진입 스캔 중인
        watching 에서는 _check_signal 이 이미 스냅샷을 떨구므로 호출하지 않는다.
        """
        iv = settings.leader_interval_min
        codes = {_bare(m["code"]) for m in self._basket}  # 전체 감시섹터 바스켓 통합됨
        codes |= self._chart_only_codes()  # §4-4 축출 섹터도 차트 유지
        for code, p in self._state.get("positions", {}).items():
            if p.get("status") == "holding":
                codes.add(_bare(code))
        for code in codes:
            try:
                bars = self.broker.get_minute_ohlcv_today(code, interval_min=iv)
                if bars:
                    chart_snapshot.write_snapshot(code, iv, bars, source="leader")
            except Exception:
                pass

    def _chart_only_codes(self) -> set[str]:
        """§4-4 차트전용 섹터(진입 감시 종료·차트만 유지) 종목 코드 집합."""
        return {
            _bare(m["code"])
            for basket in self._chart_only_sectors.values()
            for m in basket
        }

    # ── 진입 탐색 ────────────────────────────────────────────────────
    def _scan_entries(self, now: datetime) -> None:
        iv = settings.leader_interval_min
        w = settings.leader_w
        pull = settings.leader_max_pull_pct / 100
        fib = settings.leader_fib_pct  # 0=끔(고정 pull), >0=피보 되돌림 floor 로 대체
        skipped = self._state.setdefault("skipped", {})  # code → 사유 (재평가 안 함)
        positions = self._state.setdefault("positions", {})
        max_pos = max(1, settings.leader_max_positions)

        # 코드별 섹터 start_time 매핑 (섹터 추가 시각이 다를 수 있음)
        code_start: dict[str, tuple[int, int]] = {}
        for s_name, s_basket in self._sector_baskets.items():
            s_start = self._sector_start_times.get(s_name, self._trade_start)
            for m in s_basket:
                code_start.setdefault(_bare(m["code"]), s_start)

        for m in self._basket:  # rank 순 → 동시 신호 시 순위 우선
            code = _bare(m["code"])
            if code in positions:  # 오늘 이미 진입한(보유중이거나 종료된) 종목 — 재진입 없음
                continue
            if code in skipped:
                # 보류(붕괴컷 등)된 종목도 차트 스냅샷은 계속 떨군다 — 안 그러면
                # _check_signal 이 다시 안 불려 그 종목(또는 바스켓 전체 보류 시
                # 차트 탭 전체)이 멈춘다. 표시 전용 — 재평가는 하지 않는다.
                try:
                    bars = self.broker.get_minute_ohlcv_today(code, interval_min=iv)
                    if bars:
                        chart_snapshot.write_snapshot(code, iv, bars, source="leader")
                except Exception:
                    pass
                continue
            try:
                sig = self._check_signal(
                    code, now, iv, w, pull, fib,
                    trade_start=code_start.get(code),
                )
            except Exception as e:
                logger.warning("leader_trader: {} 신호 평가 실패 — {}", self._disp(code), e)
                continue
            if sig is None:
                continue
            if sig.get("skip"):  # 그날 영구 보류 (붕괴컷 등)
                skipped[code] = sig["skip"]
                self._save_state()
                logger.info("leader_trader: {} 보류 — {}", self._disp(code), sig["skip"])
                continue
            if sig.get("soft_skip"):  # 이번 신호만 스킵 (상한가컷 — 다음 스윙저점은 가능)
                key = f"{code}:{sig['bar_time']}"
                if self._soft_logged != key:
                    logger.info("leader_trader: {} 신호 스킵 — {}", self._disp(code), sig["soft_skip"])
                    self._soft_logged = key
                continue
            if sig.get("near_miss"):  # 스윙저점은 잡았으나 마지막 관문 미충족 — 기록만
                key = f"{code}:{sig['bar_time']}"
                if key not in self._near_logged:
                    logger.info("leader_trader: {} 미진입 — {}", self._disp(code), sig["near_miss"])
                    self._near_logged.add(key)
                continue
            if self._enter(m, code, sig, now):
                if len(positions) >= max_pos:
                    return  # 슬롯 소진 — 스캔 종료
                continue  # 슬롯 여유 — 나머지 바스켓 계속 스캔

        # §4-4 차트전용 섹터: 진입 판정은 안 하되 스냅샷만 유지(watching 구간).
        co = self._chart_only_codes()
        if co:
            for code in co:
                try:
                    bars = self.broker.get_minute_ohlcv_today(code, interval_min=iv)
                    if bars:
                        chart_snapshot.write_snapshot(code, iv, bars, source="leader")
                except Exception:
                    pass

    def _check_signal(
        self, code: str, now: datetime, iv: int, w: int, pull: float,
        fib: float = 0.0, trade_start: tuple[int, int] | None = None,
    ) -> dict[str, Any] | None:
        """마지막 확정봉이 스윙저점 확정봉이면 신호 dict, 아니면 None.

        backtest simulate() 와 동일 판정: pre_high/floor → W 스윙저점 →
        NODMG 붕괴컷 → 상한가컷. 반환 {"skip": 사유} 는 그날 영구 보류.

        floor: fib=0 → pre_high×(1-pull) 고정. fib>0 → 아침 임펄스다리
        (9:00~전고점시각) 되돌림 floor = pre_high - fib×(pre_high - leg_low).
        """
        bars = self.broker.get_minute_ohlcv_today(code, interval_min=iv)
        if not bars:
            return None
        # 차트 탭용 스냅샷(표시 전용·KIS 추가호출 없음). 신호 판정에 영향 없음.
        chart_snapshot.write_snapshot(code, iv, bars, source="leader")
        asc = list(reversed(bars))  # oldest-first
        # 마지막 봉이 진행 중이면 제외 → 확정봉만
        cur_key = now.replace(minute=(now.minute // iv) * iv, second=0, microsecond=0)
        cur_hms = cur_key.strftime("%H%M%S")
        while asc and asc[-1]["time"] >= cur_hms:
            asc.pop()
        n = len(asc)
        if n < 2 * w + 1:
            return None

        times = [b["time"] for b in asc]              # "HHMMSS" 문자열 비교
        lows = [b["low"] for b in asc]
        highs = [b["high"] for b in asc]
        closes = [b["close"] for b in asc]
        vols = [(b.get("volume") or 0) for b in asc]  # 라이브 분봉 거래량(#3·앵커vwap용)
        _ts = trade_start if trade_start is not None else self._trade_start
        start_hms = f"{_ts[0]:02d}{_ts[1]:02d}00"

        # AVWAP 섀도 로깅(§3단계) — 관찰 전용, 반환값은 신호 판정에 쓰지 않는다.
        _m = next((x for x in self._basket if _bare(x.get("code", "")) == code), None)
        self._avwap_probe.observe(
            code=code, name=(_m.get("name") if _m else None), bars=asc,
            meta={
                "sector": (_m.get("sector") if _m else self._active_sector_name),
                "rank": (_m.get("rank") if _m else None),
                "stock_score": (_m.get("stock_score") if _m else None),
                "entered": code in self._state.get("positions", {}),
            },
        )

        # ── 진입 모드: VWAP OR 눌림목 (2026-08-04~, VWAP 우선) ──────────────────
        # or_mode(기본): VWAP 첫눌림 신호 있으면 우선 사용, 없으면 스윙저점(눌림목) 폴백.
        # vwap_touch: VWAP 단독(폴백 없음).
        # 'pullback' 은 or_mode 별칭(하위호환).
        if settings.leader_entry_mode == "vwap_touch":
            return self._signal_vwap_touch(
                code, times, lows, highs, closes, vols, start_hms,
            )
        # OR 모드: VWAP 먼저 — 신호 있으면 즉시 반환, 없으면 눌림목 계속.
        vwap_sig = self._signal_vwap_touch(
            code, times, lows, highs, closes, vols, start_hms,
        )
        if vwap_sig is not None and not vwap_sig.get("near_miss"):
            return vwap_sig

        # 동적 앵커 시리즈(#1): off/ema/vwap. 스윙저점이 앵커 위에서 형성돼야 유효.
        # backtest_leader_pullback_v2.py 와 동일 산식.
        anchor_mode = settings.leader_anchor
        anchor: list[float] | None = None
        if anchor_mode in ("ema", "vwap", "both") and n > 0:
            ema_a: list[float] | None = None
            vwap_a: list[float] | None = None
            if anchor_mode in ("ema", "both"):
                en = max(1, settings.leader_anchor_ema)
                k = 2.0 / (en + 1)
                ema_a = [closes[0]] * n
                for t in range(1, n):
                    ema_a[t] = closes[t] * k + ema_a[t - 1] * (1 - k)
            if anchor_mode in ("vwap", "both"):
                vwap_a = [0.0] * n
                cum_pv = cum_v = 0.0
                for t in range(n):
                    tp = (highs[t] + lows[t] + closes[t]) / 3.0
                    cum_pv += tp * vols[t]
                    cum_v += vols[t]
                    vwap_a[t] = (cum_pv / cum_v) if cum_v > 0 else tp
            if anchor_mode == "ema":
                anchor = ema_a
            elif anchor_mode == "vwap":
                anchor = vwap_a
            else:  # both: 컨플루언스 — 두 앵커 중 높은 쪽 위에서 스윙저점이 형성돼야 유효
                anchor = [max(ema_a[t], vwap_a[t]) for t in range(n)]  # type: ignore[index]

        # 동적 피보 모드: 깊게 푼 밴드에 EMA 지지 가드를 덧대기 위한 EMA 시리즈(#1 보완).
        # 앵커를 별도로 켜지 않았을 때만 준비 — 켰다면 그 앵커가 우선.
        dyn_ema: list[float] | None = None
        if settings.leader_fib_dynamic and anchor is None and n > 0:
            en = max(1, settings.leader_anchor_ema)
            k = 2.0 / (en + 1)
            dyn_ema = [closes[0]] * n
            for t in range(1, n):
                dyn_ema[t] = closes[t] * k + dyn_ema[t - 1] * (1 - k)

        # Phase 1: 전고점 윈도우. phwin_min=0 → 9:00~기준시각(현행).
        # >0 → (기준시각−phwin_min분)~기준시각 롤링. 기준시각(self._trade_start)은
        # 전환 시 전환시각으로 갱신되므로(_maybe_switch), 늦은 첫선별·전환 종목도
        # 항상 '직전 phwin_min분'을 전고점 기준으로 봄 → stale 전고점 시간종속 완화.
        pw = int(getattr(settings, "leader_phwin_min", 0) or 0)
        if pw > 0:
            _ref_m = self._trade_start[0] * 60 + self._trade_start[1]
            _lo_m = max(0, _ref_m - pw)
            lo_hms = f"{_lo_m // 60:02d}{_lo_m % 60:02d}00"
            ph_idx = [j for j in range(n) if lo_hms <= times[j] < start_hms]
        else:
            ph_idx = [j for j in range(n) if times[j] < start_hms]
        if not ph_idx:
            return vwap_sig  # OR 모드: pullback 무신호 시 vwap near_miss 살려 로깅
        ph_j = max(ph_idx, key=lambda j: highs[j])
        pre_high = highs[ph_j]
        if settings.leader_fib_dynamic:
            # 동적 피보(관측용): 아침 임펄스다리 상승강도로 되돌림 깊이 자동결정.
            leg_low = min(lows[k] for k in ph_idx if k <= ph_j)
            strength = (pre_high / leg_low - 1) * 100 if leg_low > 0 else 0.0
            fib = _dynamic_fib(strength)   # 0(고정pull) / 0.382 / 0.5
        if fib > 0:
            # 피보 되돌림 floor: 아침 임펄스다리(9:00~전고점시각) 저점 기준.
            # backtest_leader_pullback_v2.py 와 동일 산식.
            leg_low = min(lows[k] for k in ph_idx if k <= ph_j)
            floor = pre_high - fib * (pre_high - leg_low)
        else:
            floor = pre_high * (1 - pull)

        # Phase 2: 마지막 확정봉 j 가 스윙저점(i = j-w) 확정봉인지
        j = n - 1
        if times[j] < start_hms:
            return vwap_sig  # OR 모드: pullback 무신호 시 vwap near_miss 살려 로깅
        i = j - w
        if not (
            i >= w
            and all(lows[i] <= lows[i - k] for k in range(1, w + 1))
            and all(lows[i] <= lows[i + k] for k in range(1, w + 1))
            and lows[i] >= floor
        ):
            # 관망 로그(확정봉마다 1회) — 왜 아직 진입 신호가 없는지 가시화(관전·검증용).
            # 판정 결과는 무신호(None)로 동일하며 로그만 남긴다.
            # 키에 ':pull' 접미 — VWAP 관망(':vwap')과 별도 슬롯이라 OR 모드에서
            # 두 경로 관망이 같은 봉에 각각 1회씩 찍힌다(눌림목이 실제로 매 봉 평가됨을 가시화).
            wkey = f"{code}:{times[j]}:pull"
            if i >= w and wkey not in self._watch_logged:
                self._watch_logged.add(wkey)
                is_min = (
                    all(lows[i] <= lows[i - k] for k in range(1, w + 1))
                    and all(lows[i] <= lows[i + k] for k in range(1, w + 1))
                )
                why = (
                    f"스윙저점 후보 {lows[i]:,.0f} < floor {floor:,.0f} (눌림 과다)"
                    if is_min and lows[i] < floor
                    else f"스윙저점 미형성 (확정봉 저가 {lows[j]:,.0f})"
                )
                logger.info(
                    "leader_trader: {} 관망 — 확정봉 {} · 전고 {:,.0f} / floor {:,.0f} — {}",
                    self._disp(code), times[j][:4], pre_high, floor, why,
                )
            return vwap_sig  # OR 모드: pullback 무신호 시 vwap near_miss 살려 로깅
        # 붕괴컷: 전고점 이후 진입 전 floor 를 깼으면 그날 보류
        if any(lows[k] < floor for k in range(ph_j + 1, j)):
            return {"skip": f"붕괴컷 (floor {floor:,.0f} 이탈)"}
        # 회복확인: 확정봉 종가가 직전봉 고가를 넘어야 진입 (터치 아닌 반등 확인).
        # 미충족 시 이번 봉만 무신호 — 다음 확정봉에서 재평가.
        # near_miss: 스윙저점까지 확정됐는데 마지막 관문에서 버린 것 — 판정 결과는
        # 무신호와 동일하나 로그에 사유를 남겨 전략 정상 동작을 검증할 수 있게 한다.
        if settings.leader_reclaim and not (closes[j] > highs[j - 1]):
            return {"near_miss":
                    f"스윙저점 {lows[i]:,.0f} 확정, 회복확인 미충족 "
                    f"(종가 {closes[j]:,.0f} ≤ 직전고가 {highs[j - 1]:,.0f}) — 다음 봉 재평가",
                    "bar_time": times[j]}
        # 장대양봉컷: 확정봉이 너무 길면(수직 회복봉) 스윙저점에서 멀어진 꼭대기
        # 진입이 되어 손절폭이 과대 → 진입 차단. 이번 봉만 무신호, 다음 봉 재평가.
        if settings.leader_bar_range_pct > 0 and lows[j] > 0 and (
            (highs[j] - lows[j]) / lows[j] * 100 > settings.leader_bar_range_pct
        ):
            return {"near_miss":
                    f"스윙저점 {lows[i]:,.0f} 확정, 장대양봉컷 "
                    f"(봉폭 {(highs[j] - lows[j]) / lows[j] * 100:.1f}% > "
                    f"{settings.leader_bar_range_pct:g}%) — 다음 봉 재평가",
                    "bar_time": times[j]}
        # 동적 앵커컷(#1): 스윙저점이 앵커(EMA·VWAP) 아래면 미진입. 고정 전고점의
        # 시간종속 문제를 완화 — 섹터전환 시에도 유효한 지지 기준. 다음 봉 재평가.
        # 앵커 가드: 사용자가 켠 앵커가 있으면 그것, 없으면 동적모드에서 깊게 푼
        # 밴드(fib>0)에 한해 EMA 가드를 덧댐(얕은 5% 밴드는 현행 그대로 무가드).
        guard = anchor
        guard_label = anchor_mode
        if guard is None and dyn_ema is not None and fib > 0:
            guard = dyn_ema
            guard_label = f"ema{settings.leader_anchor_ema}·동적"
        if guard is not None:
            tol = settings.leader_anchor_tol / 100
            floor_anchor = guard[i] * (1 - tol)
            if lows[i] < floor_anchor:
                return {"near_miss":
                        f"스윙저점 {lows[i]:,.0f} 확정, 앵커({guard_label}) 이탈 "
                        f"({lows[i]:,.0f} < {floor_anchor:,.0f}) — 다음 봉 재평가",
                        "bar_time": times[j]}
        # 경량 거래량 필터(#3): 스윙저점봉 거래량이 아침임펄스 평균 대비 과대면 미진입
        # (마른 눌림 선호). 다음 봉 재평가.
        vf = settings.leader_volfilter
        if vf > 0:
            leg_vols = [vols[k] for k in ph_idx if k <= ph_j and vols[k] > 0]
            if leg_vols:
                leg_vol = sum(leg_vols) / len(leg_vols)
                if vols[i] > vf * leg_vol:
                    return {"near_miss":
                            f"스윙저점 {lows[i]:,.0f} 확정, 거래량필터 초과 "
                            f"(저점봉 {vols[i]:,.0f} > {vf:g}×임펄스평균 {leg_vol:,.0f}) "
                            f"— 다음 봉 재평가",
                            "bar_time": times[j]}

        ref = lows[i]
        entry_est = closes[j]  # 확정봉 종가 (실체결은 시장가)
        stop = ref * (1 - settings.leader_stop_buf_pct / 100)
        tp_px = entry_est * (1 + settings.leader_tp_pct / 100)

        # 상한가컷: 전일종가 = 현재가 / (1 + 등락률)
        quote = self.broker.get_quote(code)
        prev_close = (
            quote.price / (1 + quote.change_pct / 100) if quote.change_pct > -99 else 0
        )
        if prev_close and tp_px > prev_close * 1.30:
            return {"soft_skip":
                    f"상한가컷 (목표 {tp_px:,.0f} > 상한 {prev_close * 1.30:,.0f})",
                    "bar_time": times[j]}

        return {
            "ref": ref, "stop": stop, "entry_est": entry_est,
            "pre_high": pre_high, "price_now": quote.price,
            "bar_time": times[j], "src": "pullback",
        }

    def _signal_vwap_touch(
        self,
        code: str,
        times: list[str],
        lows: list[float],
        highs: list[float],
        closes: list[float],
        vols: list[float],
        start_hms: str,
    ) -> dict[str, Any] | None:
        """교과서 VWAP 첫 눌림목 진입 판정 (leader_entry_mode='vwap_touch').

        backtest_leader_pullback_v2.py BT_MODE=textbook 와 동일 산식.
        마지막 확정봉 j 가 아래 3조건을 모두 충족하면 진입:
          (a) 직전봉 종가 > 직전봉 VWAP  — 상승추세(VWAP 위)
          (b) 이번봉 저가 ≤ VWAP×(1+tol) — VWAP 터치 (tol=leader_vwap_tol%)
          (c) 이번봉 종가 ≥ VWAP          — 되받음·지지 확인
          (d) 붕괴컷 — 이번봉 종가 ≥ 당일 전고점×(1-vwap_max_pull%)
              (leader_vwap_max_pull_pct, 0=끔). 깊게 밀린 VWAP 터치는
              눌림목이 아니라 추세붕괴라 반등 확률이 역전됨.
          (e) 기울기컷 — 최근 5봉 VWAP 상승률 ≥ vwap_min_slope%
              (leader_vwap_min_slope_pct, 기본 0=끔 — 관측만).
        (c) 통과 시점마다 기울기·눌림 관측 로그(vwap_probe)를 한 줄 남긴다
        (컷 적용 여부와 무관 — 나중에 로그만으로 사후 검증하기 위함).
        진입가=이번봉 종가, 참조저점=이번봉 저가, 손절=참조×(1-stop_buf%),
        익절=진입가×(1+tp%). 장대양봉컷·상한가컷·하루1종목·마감청산은 pullback 과 동일.
        스윙저점·회복확인(직전고가 돌파)은 미사용.
        """
        n = len(closes)
        j = n - 1
        if j < 1 or times[j] < start_hms:
            return None

        # 세션 VWAP(9:00~ 누적, TP=(고+저+종)/3 거래량가중)
        vwap = [0.0] * n
        cum_pv = cum_v = 0.0
        for t in range(n):
            tp = (highs[t] + lows[t] + closes[t]) / 3.0
            cum_pv += tp * vols[t]
            cum_v += vols[t]
            vwap[t] = (cum_pv / cum_v) if cum_v > 0 else tp

        tol = settings.leader_vwap_tol / 100    # VWAP 터치 허용오차(전용 파라미터)
        v, vprev = vwap[j], vwap[j - 1]

        # (a) 직전봉 VWAP 위
        if not (closes[j - 1] > vprev):
            # 키에 ':vwtrend' 접미 — VWAP 미터치(':vwap')와 별도 슬롯.
            # 이 조건 탈락은 로그 없이 조용히 눌림목 로직으로 넘어가던 것을
            # 가시화 — 안 그러면 순위가 낮아 VWAP 아래에 눌린 종목은 VWAP
            # 관련 로그가 아예 안 찍혀 "왜 얘만 VWAP 로그가 없냐"는 착시가 생김.
            wkey = f"{code}:{times[j]}:vwtrend"
            if wkey not in self._watch_logged:
                self._watch_logged.add(wkey)
                logger.info(
                    "leader_trader: {} 관망(vwap) — 확정봉 {} · 직전봉 종가 {:,.0f} ≤ "
                    "직전봉 VWAP {:,.0f} — 상승추세 아님(VWAP 아래)",
                    self._disp(code), times[j][:4], closes[j - 1], vprev,
                )
            return None

        # (b) 이번봉 VWAP 터치
        if not (lows[j] <= v * (1 + tol)):
            # 키에 ':vwap' 접미 — 눌림목 관망(':pull')과 별도 슬롯(위 _signal 참고).
            wkey = f"{code}:{times[j]}:vwap"
            if wkey not in self._watch_logged:
                self._watch_logged.add(wkey)
                logger.info(
                    "leader_trader: {} 관망(vwap) — 확정봉 {} · VWAP {:,.0f} · "
                    "저가 {:,.0f} — 아직 VWAP 미터치",
                    self._disp(code), times[j][:4], v, lows[j],
                )
            return None

        # (c) 이번봉 되받음(종가 ≥ VWAP)
        if not (closes[j] >= v):
            return {
                "near_miss":
                    f"VWAP 터치 {lows[j]:,.0f}≤{v:,.0f} 확정, 되받음 미충족 "
                    f"(종가 {closes[j]:,.0f} < VWAP {v:,.0f}) — 다음 봉 재평가",
                "bar_time": times[j],
            }

        # ── VWAP 기울기 관측 (2026-08-18) ───────────────────────────────
        # 기울기 = 최근 _SLOPE_BARS 봉 동안 VWAP 자체가 몇 % 올랐나.
        # VWAP 은 9시부터의 누적 거래량가중 평균이라 관성이 크다 → 이걸 밀어
        # 올렸다는 건 신규 물량이 "당일 평균가보다 확실히 위에서" 체결됐다는 뜻
        # (실매집). 기울기가 0/음수면 같은 VWAP 터치라도 판이 죽어가는 중이라
        # 지지선 자체가 계속 내려온다.
        # 스윕(5종목×7거래일)에서 ≥+0.2% 구간 승률 72.7% 가 나왔지만 n=11 이라
        # 채택은 보류 — 기본값 0(끔)으로 두고 아래 관측 로그만 쌓아 사후 검증한다.
        _sb = _SLOPE_BARS if j >= _SLOPE_BARS else j
        _v0 = vwap[j - _sb]
        vw_slope = ((v / _v0 - 1) * 100) if _v0 > 0 else 0.0
        _ph = max(highs[:j]) if j >= 1 else 0.0
        _pull_now = ((closes[j] / _ph - 1) * 100) if _ph > 0 else 0.0

        pkey = f"{code}:{times[j]}:vwprobe"
        if pkey not in self._watch_logged:
            self._watch_logged.add(pkey)
            logger.info(
                "leader_trader: {} vwap_probe — 확정봉 {} · VWAP {:,.0f} · "
                "기울기({}봉) {:+.2f}% · 전고점대비 {:+.1f}% · 종가 {:,.0f}",
                self._disp(code), times[j][:4], v, _sb, vw_slope,
                _pull_now, closes[j],
            )

        # 장대양봉컷 (pullback 과 동일)
        if (
            settings.leader_bar_range_pct > 0 and lows[j] > 0
            and (highs[j] - lows[j]) / lows[j] * 100 > settings.leader_bar_range_pct
        ):
            return {
                "near_miss":
                    f"VWAP 되받음 확정, 장대양봉컷 "
                    f"(봉폭 {(highs[j] - lows[j]) / lows[j] * 100:.1f}% > "
                    f"{settings.leader_bar_range_pct:g}%) — 다음 봉 재평가",
                "bar_time": times[j],
            }

        # 붕괴컷 (2026-08-18): 전고점 대비 깊게 밀린 터치는 눌림목이 아니라 추세붕괴.
        # or_mode 의 leader_max_pull_pct 는 스윙저점 floor 라 VWAP 분기에는 안 걸리므로
        # 전용 파라미터로 분리한다(두 모드의 VWAP 분기에 공통 적용 — or_mode 에서 컷에
        # 걸리면 near_miss 로 반환돼 기존대로 스윙저점 폴백으로 넘어간다).
        # 전고점 = 9:00~직전봉 최고가(이번봉 제외 — 터치봉 자체가 고점을 갱신하면
        # 눌림이 0 이 돼 컷이 무력화됨).
        _mp = settings.leader_vwap_max_pull_pct
        if _mp > 0 and j >= 1:
            pre_high = max(highs[:j])
            if pre_high > 0:
                pull = (closes[j] / pre_high - 1) * 100
                if pull < -_mp:
                    return {
                        "near_miss":
                            f"VWAP 되받음 확정, 붕괴컷 "
                            f"(전고점 {pre_high:,.0f} 대비 {pull:+.1f}% < -{_mp:g}%) "
                            f"— 얕은 눌림 아님, 다음 봉 재평가",
                        "bar_time": times[j],
                    }

        # 기울기컷 (2026-08-18, 기본 끔). 표본이 얇아 기본값 0 — 로그로 검증 후 판단.
        _ms = settings.leader_vwap_min_slope_pct
        if _ms > 0 and vw_slope < _ms:
            return {
                "near_miss":
                    f"VWAP 되받음 확정, 기울기컷 "
                    f"(VWAP {_sb}봉 기울기 {vw_slope:+.2f}% < +{_ms:g}%) "
                    f"— VWAP 정체·하락 중, 다음 봉 재평가",
                "bar_time": times[j],
            }

        ref = lows[j]                       # 참조저점 = 터치봉 저가
        entry_est = closes[j]               # 확정봉 종가 (실체결은 시장가)
        stop = ref * (1 - settings.leader_stop_buf_pct / 100)
        tp_px = entry_est * (1 + settings.leader_tp_pct / 100)

        # 상한가컷: 전일종가 = 현재가 / (1 + 등락률)
        quote = self.broker.get_quote(code)
        prev_close = (
            quote.price / (1 + quote.change_pct / 100) if quote.change_pct > -99 else 0
        )
        if prev_close and tp_px > prev_close * 1.30:
            return {
                "soft_skip":
                    f"상한가컷 (목표 {tp_px:,.0f} > 상한 {prev_close * 1.30:,.0f})",
                "bar_time": times[j],
            }

        return {
            "ref": ref, "stop": stop, "entry_est": entry_est,
            "pre_high": max(highs),         # 표시용(전고점 개념 없음 → 세션 최고가)
            "price_now": quote.price, "bar_time": times[j], "src": "vwap",
            "vw_slope": vw_slope,           # 관측용(매매 로직 미사용)
        }

    def _enter(
        self, member: dict[str, Any], code: str, sig: dict[str, Any], now: datetime
    ) -> bool:
        src = sig.get("src", "pullback")
        entry_label = "VWAP 진입" if src == "vwap" else "눌림목 진입"
        ref_label = "VWAP 지지" if src == "vwap" else "스윙저점"
        strategy = "leader_vwap_touch" if src == "vwap" else "leader_pullback"
        price = sig["price_now"] or sig["entry_est"]
        if not price or price <= 0:
            logger.warning(
                "leader_trader: {} 진입 스킵 — 유효 가격 없음 (price_now={}, entry_est={})",
                self._disp(code), sig.get("price_now"), sig.get("entry_est"),
            )
            self._state.setdefault("skipped", {})[code] = "가격 0/None"
            self._save_state()
            return False
        if settings.leader_slot_budget_krw > 0:
            slot_budget = settings.leader_slot_budget_krw
        else:
            slot_budget = settings.leader_budget_krw / max(1, settings.leader_max_positions)
        qty = int(slot_budget // price)
        if qty < 1:
            logger.warning(
                "leader_trader: {} 예산 부족 (슬롯예산 {:,.0f} < 현재가 {:,.0f})",
                self._disp(code), slot_budget, price,
            )
            self._state.setdefault("skipped", {})[code] = "예산 부족"
            self._save_state()
            return False
        # 관전(매매 off): 실주문 없이 가상 진입 — 상태머신·청산 판정은 실전과 동일.
        # 점유원장·record_trade 는 건드리지 않는다(진짜 자본/DB 오염 방지).
        if not settings.leader_trade_enabled:
            entry = price
            tp_px = entry * (1 + settings.leader_tp_pct / 100)
            self._state.setdefault("positions", {})[code] = {
                "status": "holding", "virtual": True,
                "symbol": code, "name": member.get("name", ""),
                "rank": member.get("rank", 1), "qty": qty,
                "entry": entry, "ref": sig["ref"], "stop": sig["stop"], "tp": tp_px,
                "entry_at": f"{now:%H:%M:%S}", "bar_time": sig["bar_time"], "src": src,
                "peak": entry,
            }
            self._save_state()
            notify(
                f"👁 **대장주봇 관전 — 가상매수** {member.get('name', '')}({code}) "
                f"x{qty} @ {entry:,.0f}\n"
                f"{ref_label} {sig['ref']:,.0f} · 손절 {sig['stop']:,.0f} · "
                f"목표 {tp_px:,.0f} (+{settings.leader_tp_pct:g}%) — 실주문 없음"
            )
            logger.info(
                "leader_trader: [관전] 가상 진입 {} x{} @ {:,.0f} (확정봉 {} / stop {:,.0f} / tp {:,.0f})",
                self._disp(code), qty, entry, sig["bar_time"][:4], sig["stop"], tp_px,
            )
            return True
        # 호가창을 먼저 읽어 '전량 체결되는' 주문을 설계한다(수량이 줄 수 있다).
        # 점유 선점보다 먼저 해야 실제 주문 수량으로 점유가 잡힌다.
        qty, ord_px, ord_type = self._plan_entry_order(code, qty, slot_budget)
        if qty < 1:
            logger.warning("leader_trader: {} 호가 잔량 0 — 진입 skip", self._disp(code))
            self._state.setdefault("skipped", {})[code] = "호가 잔량 없음"
            self._save_state()
            return False
        # 점유 선점: 스톡봇이 같은 종목을 이미 잡고 있으면 양보(더블 매수 방지).
        if settings.leader_own_symbol_priority and not position_owner.claim(code, "leader", qty):
            logger.info("leader_trader: {} [점유-양보] 스톡봇이 선점 → 진입 skip", self._disp(code))
            self._state.setdefault("skipped", {})[code] = "스톡봇 선점"
            self._save_state()
            return False
        try:
            resp = self.broker.place_order(
                code, "buy", qty, price=ord_px, order_type=ord_type)
        except OrderRejectedError as e:
            if settings.leader_own_symbol_priority:
                position_owner.release(code, "leader")  # 미체결 점유 회수
            notify(f"🚫 **대장주봇 매수 거부** {member.get('name', '')}({code}) x{qty}: {e}")
            self._state.setdefault("skipped", {})[code] = f"주문 거부: {e}"
            self._save_state()
            return False

        # ── 실제 체결수량 확인 ──────────────────────────────────────
        # 주문 접수 응답("매수주문이 완료되었습니다")은 체결 보장이 아니다.
        # 2026-08-27 HD현대일렉트릭: 시장가 62주가 호가 잔량 부족으로 31주만
        # 체결됐는데 상태는 62 로 기록돼, 1차 분할익절(31주)이 잔량을 전부 털었다.
        # 그 뒤 '잔량 31주' 매도가 매 틱 [40240000] 잔고내역 없음으로 거부되며
        # 디스코드가 도배됐다. 주문 직후 실제 체결수량으로 상태를 맞춘다.
        # 조회 실패(None)면 보정하지 않는다 — 주문 수량 그대로 두고, 어긋나면
        # 나중에 _on_sell_reject 가 잔고와 대조해 잡는다.
        fill = None
        # 지정가(호가 스윕)는 잔량이 호가창에 남아 시간을 두고 채워진다. 3초 뒤
        # 한 번만 보고 확정하면 그 순간의 부분체결이 '최종'이 되고, 아래에서
        # 잔량을 취소해버린다 — 2026-08-28 003350 은 3401주 중 30주만 남기고
        # 3371주를 날려 5천만원 진입이 44만원이 됐다. 전량 채워질 때까지(최대
        # 약 20초) 기다린 뒤 확정한다. 시장가는 잔량이 즉시 취소되므로 그대로.
        _fill_kw = ({"attempts": 10, "wait": 2.0, "until_complete": True}
                    if ord_type == "limit" else {})
        try:
            fill = self.broker.get_order_fill(code, resp, **_fill_kw)
        except Exception as e:
            logger.warning("leader_trader: 체결 조회 실패 {} — {}", self._disp(code), e)
        if fill is not None:
            filled = int(fill.get("filled_qty", 0) or 0)
            if filled <= 0 and ord_type == "limit":
                # ── 지정가 전량 미체결 ────────────────────────────────────
                # 시장가는 미체결분이 자동 취소되지만 지정가는 호가창에 그대로
                # **남는다**. 여기서 그냥 진입 취소하면 몇 초 뒤 늦게 체결돼
                # '봇은 포지션 없음 / 계좌엔 물량 있음' 이 된다 — 손절·익절도
                # 감시도 없는 방치 물량이라 원래 사고보다 나쁘다.
                # ① 먼저 취소한다.
                cancelled = False
                try:
                    cancelled = self.broker.cancel_order(resp)
                except Exception as e:
                    logger.warning("leader_trader: 미체결 취소 예외 {} — {}", self._disp(code), e)
                if not cancelled:
                    # 취소가 안 됐다 = 그 사이 체결됐을 가능성이 높다. 재조회.
                    try:
                        fill2 = self.broker.get_order_fill(code, resp)
                    except Exception:
                        fill2 = None
                    f2 = int((fill2 or {}).get("filled_qty", 0) or 0)
                    if f2 > 0:
                        fill, filled = fill2, f2
                    else:
                        # 취소 실패 + 체결 0 = 상태를 확신할 수 없다. 포지션을
                        # 만들지도 재시도하지도 않고 사람에게 넘긴다.
                        if settings.leader_own_symbol_priority:
                            position_owner.release(code, "leader")
                        notify(
                            f"🚨 **대장주봇 미체결 주문 취소 실패** {member.get('name', '')}({code}) "
                            f"x{qty} @ {ord_px:,.0f} 지정가 — 체결 0주인데 취소도 거부됐습니다. "
                            f"증권사 앱에서 잔여 주문을 직접 확인해 주세요."
                        )
                        logger.error("leader_trader: 미체결 취소 실패 {} x{} — 수동 확인 필요",
                                     self._disp(code), qty)
                        self._state.setdefault("skipped", {})[code] = "미체결 취소 실패"
                        self._save_state()
                        return False
                # ② 취소가 됐다면 '못 사는' 실패를 남기지 않는다 — 시장가로 한 번
                #    재시도해 기존 동작으로 되돌린다. 지정가는 호가를 읽고 주문이
                #    닿는 사이 호가가 위로 뛰면 한 주도 안 사는데, 눌림목 회복
                #    진입은 그 순간이 가장 빠르게 움직인다.
                if filled <= 0:
                    logger.warning(
                        "leader_trader: {} 지정가 {:,.0f} 미체결 — 시장가 재시도 x{}주",
                        self._disp(code), ord_px, qty,
                    )
                    try:
                        resp = self.broker.place_order(code, "buy", qty, order_type="market")
                        ord_type = "market"
                    except OrderRejectedError as e:
                        if settings.leader_own_symbol_priority:
                            position_owner.release(code, "leader")
                        notify(f"🚫 **대장주봇 매수 거부(시장가 재시도)** {member.get('name', '')}({code}) x{qty}: {e}")
                        self._state.setdefault("skipped", {})[code] = f"주문 거부: {e}"
                        self._save_state()
                        return False
                    try:
                        fill = self.broker.get_order_fill(code, resp)
                    except Exception as e:
                        logger.warning("leader_trader: 재시도 체결 조회 실패 {} — {}", self._disp(code), e)
                        fill = None
                    filled = int((fill or {}).get("filled_qty", 0) or 0)
                    if fill is None:
                        # 조회 실패 — 보정하지 않고 주문 수량 그대로 둔다(기존 규칙).
                        filled = qty
            if fill is not None and filled <= 0:
                # 전량 미체결 — 포지션을 만들지 않는다(만들면 유령 잔량이 생긴다).
                if settings.leader_own_symbol_priority:
                    position_owner.release(code, "leader")
                notify(
                    f"⚠️ **대장주봇 매수 미체결** {member.get('name', '')}({code}) "
                    f"x{qty} @ {price:,.0f} — 체결 0주, 진입 취소"
                )
                logger.warning("leader_trader: 매수 미체결 {} x{} — 진입 취소",
                               self._disp(code), qty)
                self._state.setdefault("skipped", {})[code] = "매수 미체결"
                self._save_state()
                return False
            if fill is not None and filled != qty:
                logger.warning(
                    "leader_trader: 부분체결 보정 {} 주문 {}주 → 체결 {}주 (평균 {:,.0f})",
                    self._disp(code), qty, filled, fill.get("avg_price", 0) or 0,
                )
                # 지정가는 시장가와 달리 미체결 잔량이 호가창에 살아남는다.
                # 몇 초 뒤 뒤늦게 체결되면 상태(=filled)와 브로커 잔고가 다시
                # 어긋나므로, 보정 전에 잔량부터 취소한다.
                if ord_type == "limit":
                    try:
                        if not self.broker.cancel_order(resp):
                            notify(
                                f"⚠️ **대장주봇 잔량 취소 실패** {member.get('name', '')}({code}) "
                                f"미체결 {qty - filled}주 — 증권사 앱에서 확인이 필요합니다."
                            )
                    except Exception as e:
                        logger.warning("leader_trader: 잔량 취소 예외 {} — {}", self._disp(code), e)
                notify(
                    f"⚠️ **대장주봇 부분체결** {member.get('name', '')}({code}) "
                    f"주문 {qty}주 → 실제 {filled}주 — 보유수량을 실제값으로 기록합니다."
                )
                if settings.leader_own_symbol_priority:
                    # 점유 수량도 실제 체결분으로 맞춘다(미체결분 반납).
                    try:
                        position_owner.release(code, "leader")
                        position_owner.claim(code, "leader", filled)
                    except Exception:
                        pass
                qty = filled

        # 진입가: 실제 체결 평균단가가 있으면 그것을 쓴다. 스윕 지정가는 여러
        # 호가에 걸쳐 체결되므로 현재가(price)와 벌어질 수 있고, 그대로 두면
        # 익절가·손익률이 실제와 어긋난다. 조회 실패 시에만 현재가로 폴백.
        entry = price
        if fill is not None:
            _avg = float(fill.get("avg_price", 0) or 0)
            if _avg > 0:
                entry = _avg
        tp_px = entry * (1 + settings.leader_tp_pct / 100)
        self._state.setdefault("positions", {})[code] = {
            "status": "holding",
            "symbol": code,
            "name": member.get("name", ""),
            "rank": member.get("rank", 1),
            "qty": qty,
            "entry": entry,
            "ref": sig["ref"],
            "stop": sig["stop"],
            "tp": tp_px,
            "entry_at": f"{now:%H:%M:%S}",
            "bar_time": sig["bar_time"],
            "src": src,
            "peak": entry,
        }
        self._save_state()
        record_trade(
            symbol=code, side="buy", quantity=qty, price=entry,
            reason=f"{entry_label} ({ref_label} {sig['ref']:,.0f}, 확정봉 {sig['bar_time'][:4]})",
            broker_response=json.dumps(resp, ensure_ascii=False)[:500],
            strategy=strategy,
            details={"ref": sig["ref"], "stop": sig["stop"], "tp": tp_px,
                     "pre_high": sig["pre_high"], "rank": member.get("rank", 1)},
        )
        notify(
            f"🟢 **대장주봇 매수** {member.get('name', '')}({code}) x{qty} @ {entry:,.0f}\n"
            f"{ref_label} {sig['ref']:,.0f} · 손절 {sig['stop']:,.0f} · "
            f"목표 {tp_px:,.0f} (+{settings.leader_tp_pct:g}%)"
        )
        logger.info(
            "leader_trader: 진입 {} x{} @ {:,.0f} (stop {:,.0f} / tp {:,.0f})",
            self._disp(code), qty, entry, sig["stop"], tp_px,
        )
        return True

    # ── 진입 주문 설계(호가 스윕) ────────────────────────────────────
    def _plan_entry_order(
        self, code: str, qty: int, budget: float = 0.0,
    ) -> tuple[int, float, str]:
        """주문 직전 호가창을 읽어 '원하는 수량이 다 채워지는' 가격을 찾는다.

        시장가(01)는 최우선 매도호가 잔량만큼만 체결되고 나머지는 취소된다.
        2026-08-27 HD현대일렉트릭: 62주 시장가가 1호가 잔량 31주에 막혀 절반만
        체결됐다. 미체결분을 나중에 따로 사면 부분체결 상태 구간이 생기므로,
        아예 처음부터 **전량이 채워지는 호가까지 긁는 지정가**로 보낸다.
        매수 지정가는 최우선 매도호가보다 높아도 그 아래 호가부터 순서대로
        체결되므로, 실제 평균단가는 계산한 상한가보다 낮거나 같다.

        Returns: (수량, 주문가, order_type)
          · ("limit", 스윕가)  — 정상. 상한 안에서 qty 전량 커버.
          · ("market", 0.0)    — 호가 조회 실패/스윕 off → **기존 시장가 폴백**.
        상한(leader_sweep_max_slip_pct) 안에서 잔량이 모자라면 수량을 커버
        가능한 만큼 줄인다(진입 자체를 포기하지 않는다).
        """
        if not settings.leader_sweep_enabled:
            return qty, 0.0, "market"
        book: dict[str, Any] = {}
        try:
            book = self.broker.get_orderbook(code) or {}
        except Exception as e:  # get_orderbook 은 {} 를 주지만 방어
            logger.warning("leader_trader: 호가 조회 예외 {} — {}", self._disp(code), e)
        asks = book.get("asks") or []
        if not asks:
            # 폴백: 호가를 못 읽었다고 진입을 포기하지 않는다. 시장가로 보내고,
            # 부분체결이 나면 아래 get_order_fill 보정이 수량을 실제값으로 맞춘다.
            logger.warning("leader_trader: {} 호가 조회 실패 — 시장가 폴백", self._disp(code))
            return qty, 0.0, "market"

        best = float(asks[0]["price"])
        cap = best * (1 + settings.leader_sweep_max_slip_pct / 100)

        def _walk(want: int) -> tuple[int, float]:
            """상한 안에서 want 주를 채우는 데 필요한 호가까지 누적."""
            cum, px = 0, best
            for lv in asks:
                lv_px = float(lv["price"])
                if lv_px > cap:
                    break
                cum += int(lv["qty"])
                px = lv_px
                if cum >= want:
                    break
            return min(cum, want) if cum >= want else cum, px

        cum, sweep_px = _walk(qty)
        if cum < 1:
            return qty, 0.0, "market"

        # 예산 상한: 수량은 현재가 기준으로 뽑혔는데(qty = 예산 // 현재가) 스윕가는
        # 1호가보다 최대 slip% 높다. 그대로 두면 qty x sweep_px 가 예산을 넘어
        # '부분체결'이 아니라 **주문 전체가 증거금 부족으로 거부**될 수 있다.
        # 대장주는 슬롯 전액을 넣으므로 여유가 없다 — 살 수 있는 수량으로 깎는다.
        if budget > 0 and sweep_px > 0:
            affordable = int(budget // sweep_px)
            if affordable < 1:
                logger.warning(
                    "leader_trader: {} 예산 {:,.0f}원으로 스윕가 {:,.0f}원 1주도 불가 — 진입 skip",
                    self._disp(code), budget, sweep_px,
                )
                return 0, 0.0, "limit"
            if affordable < qty:
                logger.info(
                    "leader_trader: {} 예산 상한 — {}주 → {}주 (스윕가 {:,.0f} x {}주 > 예산 {:,.0f})",
                    self._disp(code), qty, affordable, sweep_px, qty, budget,
                )
                qty = affordable
                # 수량이 줄었으니 더 얕은 호가로 끝날 수 있다 — 다시 걷는다.
                cum, sweep_px = _walk(qty)
                if cum < 1:
                    return qty, 0.0, "market"
        if cum < qty:
            logger.warning(
                "leader_trader: {} 호가 잔량 부족 — 요청 {}주 / 상한 {:,.0f}원(+{:g}%)까지 {}주 → 수량 축소",
                self._disp(code), qty, cap, settings.leader_sweep_max_slip_pct, cum,
            )
            notify(
                f"⚠️ **대장주봇 호가 잔량 부족** ({code}) 요청 {qty}주 → {cum}주로 축소\n"
                f"1호가 {best:,.0f} · 슬리피지 상한 +{settings.leader_sweep_max_slip_pct:g}%"
                f"({cap:,.0f}) 안에서 살 수 있는 최대 수량입니다."
            )
            qty = cum
        if sweep_px > best:
            logger.info(
                "leader_trader: {} 호가 스윕 — 1호가 {:,.0f} → 지정가 {:,.0f} ({:+.2f}%) x{}주",
                self._disp(code), best, sweep_px, (sweep_px / best - 1) * 100, qty,
            )
        return qty, sweep_px, "limit"

    # ── 매도 거부 처리 ───────────────────────────────────────────────
    def _broker_qty(self, code: str) -> int | None:
        """브로커 실제 보유수량. 조회 실패면 None(=판단 보류, 상태 건드리지 않음)."""
        bc = _bare(code)
        try:
            rows = self.broker.get_positions()
        except Exception as e:
            logger.warning("leader_trader: 잔고 조회 실패 {} — {}", self._disp(code), e)
            return None
        for row in rows:
            if str(row.get("pdno", "")).strip() == bc:
                return int(row.get("hldg_qty", 0) or 0)
        return 0

    def _notify_reject(self, code: str, msg: str) -> None:
        """같은 거부 메시지의 반복 알림 억제. 메시지가 바뀌거나 유예가 지나면 다시 보낸다."""
        prev = self._sell_reject.get(code)
        ts = _time.time()
        if prev and prev[0] == msg and ts - prev[1] < _REJECT_NOTIFY_SEC:
            return
        self._sell_reject[code] = (msg, ts)
        try:
            notify(msg)
        except Exception:
            pass

    def _on_sell_reject(
        self, st: dict[str, Any], code: str, sell_qty: int, price: float,
        now: datetime, reason: str, err: Exception,
    ) -> bool:
        """매도 거부 공통 처리. True 면 포지션을 종료 처리했으므로 재시도가 없다.

        2026-08-27 HD현대일렉트릭(267260): 62주 매수 주문이 실제로는 31주만 잡혔는데
        상태는 62 로 남아, 1차 분할익절(31주)이 잔량을 전부 털었다. 그 뒤 '잔량 31주'
        매도가 매 틱마다 [40240000] 모의투자 잔고내역이 없습니다 로 거부되며 디스코드에
        무한 도배됐다. 거부는 그동안 `return  # 다음 틱 재시도` 뿐이라 끊길 방법이
        없었다 — 거부를 만나면 브로커 실제 잔고와 대조해 상태를 바로잡는다.
        """
        name = st.get("name", "")
        logger.error("leader_trader: 매도 거부 {} x{} [{}] — {}",
                     self._disp(code), sell_qty, reason, err)
        held = self._broker_qty(code)
        base = f"🚫 **대장주봇 매도 거부** {name}({code}) x{sell_qty}: {err}"
        if held is None:  # 잔고 조회 실패 — 상태 유지, 다음 틱 재시도
            self._notify_reject(code, base)
            return False

        if held <= 0:
            # 브로커에 물량이 없다 = 팔 것이 없다. 재시도해도 영원히 거부된다.
            entry = float(st["entry"])
            net = (price * (1 - _SELL_COMM) / (entry * (1 + _BUY_COMM)) - 1) * 100
            st.update({
                "status": "done", "exit": price, "exit_at": f"{now:%H:%M:%S}",
                "exit_reason": f"{reason}(잔고없음)", "net_pct": round(net, 2),
            })
            self._save_state()
            if settings.leader_own_symbol_priority:
                position_owner.release(code, "leader")
            # 실제 매도가 없었으므로 record_trade 는 하지 않는다(진짜 손익=브로커 net_pnl).
            self._notify_reject(
                code,
                f"⚠️ **대장주봇 포지션 정리** {name}({code})\n"
                f"상태는 {sell_qty}주 보유였으나 브로커 잔고 0 — 매도 불가로 종료 처리.\n"
                f"사유: {err}"
            )
            logger.warning(
                "leader_trader: 잔고 0 확인 — {} 포지션 종료 처리 (상태 {}주)",
                self._disp(code), sell_qty,
            )
            return True

        total = int(st.get("qty", 0) or 0)
        if held != total:
            # 상태 수량이 실제와 다르다 → 실제값으로 보정하고 다음 틱에 재매도.
            st["qty"] = held
            self._save_state()
            self._notify_reject(
                code,
                f"⚠️ **대장주봇 수량 보정** {name}({code}) 상태 {total}주 → 실제 {held}주\n"
                f"다음 틱에 실제 수량으로 재매도합니다."
            )
            logger.warning("leader_trader: 수량 보정 {} {}주 → {}주",
                           self._disp(code), total, held)
            return False

        # 수량은 맞는데 거부 — 일시적 사유(장 마감·유량 등). 알림만 억제하고 재시도.
        self._notify_reject(code, base)
        return False

    # ── 매도 체결 확인 ───────────────────────────────────────────────
    def _confirm_sell_fill(
        self, code: str, resp: dict[str, Any], qty: int,
    ) -> tuple[int, float]:
        """매도 주문의 실제 체결수량·평균가를 확인한다.

        시장가 매도는 최우선 매수호가 잔량만큼만 체결되고 나머지는 취소된다 —
        2026-08-27 매수 62주 사고와 **똑같은 구조가 매도에도** 있었다.
        부분체결은 '주문 거부'가 아니라서 OrderRejectedError 가 안 나고,
        따라서 _on_sell_reject 는 절대 못 잡는다. 주문 수량을 그대로 믿으면
          · 분할익절: 잔량 계산이 틀어져 이후 매도가 매 틱 [40240000] 으로 거부
          · 손절/마감청산: 포지션을 done 으로 닫아버려 계좌에 손절선도 감시도
            없는 방치 물량이 남는다(가장 위험).
        그래서 매도도 주문 직후 실제 체결수량을 확인한다.

        Returns: (체결수량, 체결평균가). 조회 실패면 (qty, 0.0) — 보정하지
        않고 주문 수량 그대로 둔다(매수와 같은 규칙: 절대 0 으로 뭉개지 않는다).
        """
        try:
            fill = self.broker.get_order_fill(code, resp)
        except Exception as e:
            logger.warning("leader_trader: 매도 체결 조회 실패 {} — {}", self._disp(code), e)
            return qty, 0.0
        if fill is None:
            return qty, 0.0
        filled = max(0, min(int(fill.get("filled_qty", 0) or 0), qty))
        avg = float(fill.get("avg_price", 0) or 0)
        return filled, avg

    def _partial_exit(
        self, st: dict[str, Any], code: str, sell_qty: int, price: float,
        now: datetime, new_stop: float,
    ) -> None:
        """[split 모드] 1차 목표가 도달 — 물량 일부만 팔고 포지션은 유지, 손절선을 1차 목표가로 상향(본전확보)."""
        entry = float(st["entry"])
        total_qty = int(st["qty"])
        remain_qty = total_qty - sell_qty
        if st.get("virtual"):
            net1 = (price * (1 - _SELL_COMM) / (entry * (1 + _BUY_COMM)) - 1) * 100
            notify(
                f"👁 **대장주봇 관전 — 가상 1차 분할익절** {st.get('name', '')}({code}) "
                f"x{sell_qty} @ {price:,.0f} (잔량 {remain_qty}주 · 손절선 {new_stop:,.0f} 로 상향/본전확보)\n"
                f"진입 {entry:,.0f} → net {net1:+.2f}% (실주문 없음)"
            )
            logger.info(
                "leader_trader: [관전] 1차 분할익절 {} x{} @ {:,.0f} net {:+.2f}% (잔량 {})",
                self._disp(code), sell_qty, price, net1, remain_qty,
            )
        else:
            try:
                resp = self.broker.place_order(code, "sell", sell_qty, order_type="market")
            except OrderRejectedError as e:
                self._on_sell_reject(st, code, sell_qty, price, now, "1차 분할익절", e)
                return  # 종료 처리됐거나(잔고 0) 다음 틱 재시도
            # 주문 접수 ≠ 체결. 실제 체결수량을 확인하고 나서야 '성공'이다.
            filled, avg_px = self._confirm_sell_fill(code, resp, sell_qty)
            if filled <= 0:
                # 한 주도 안 팔렸다. 상태를 하나도 건드리지 않고(잔량·손절선·
                # split_done 유지) 돌아간다 → 다음 틱에 같은 조건으로 재시도.
                # 매 틱 반복될 수 있으므로 알림은 억제 경유(_notify_reject).
                self._notify_reject(
                    code,
                    f"⚠️ **대장주봇 1차 분할익절 미체결** {st.get('name', '')}({code}) "
                    f"x{sell_qty} @ {price:,.0f} — 체결 0주, 다음 틱에 재시도합니다.",
                )
                logger.warning("leader_trader: 1차 분할익절 미체결 {} x{} — 재시도",
                               self._disp(code), sell_qty)
                return
            self._sell_reject.pop(code, None)  # 실제 체결 확인 → 거부 억제 해제
            if filled < sell_qty:
                logger.warning(
                    "leader_trader: 분할익절 부분체결 {} 주문 {}주 → 체결 {}주",
                    self._disp(code), sell_qty, filled,
                )
                notify(
                    f"⚠️ **대장주봇 분할익절 부분체결** {st.get('name', '')}({code}) "
                    f"주문 {sell_qty}주 → 실제 {filled}주 — 잔량을 실제값으로 기록합니다."
                )
                sell_qty = filled
                remain_qty = total_qty - filled
            if avg_px > 0:
                price = avg_px  # 기록·알림은 현재가가 아닌 실제 체결단가로
            src = st.get("src", "pullback")
            strategy = "leader_vwap_touch" if src == "vwap" else "leader_pullback"
            record_trade(
                symbol=code, side="sell", quantity=sell_qty, price=price,
                reason=f"1차 분할익절 (+{settings.leader_split_tp1_pct:g}%)",
                broker_response=json.dumps(resp, ensure_ascii=False)[:500],
                strategy=strategy,
                details={"entry": entry, "sell_qty": sell_qty, "remain_qty": remain_qty},
            )
            notify(
                f"🟢 **대장주봇 1차 분할익절** {st.get('name', '')}({code}) x{sell_qty} @ {price:,.0f} "
                f"(잔량 {remain_qty}주 · 손절선 {new_stop:,.0f} 로 상향/본전확보)"
            )
            logger.info(
                "leader_trader: 1차 분할익절 {} x{} @ {:,.0f} (잔량 {})",
                self._disp(code), sell_qty, price, remain_qty,
            )
        st["qty"] = remain_qty
        st["stop"] = new_stop
        st["split_done"] = True
        self._save_state()

    # ── 보유 관리 ────────────────────────────────────────────────────
    def _manage_position(self, code: str, now: datetime) -> None:
        st = self._state.get("positions", {}).get(code)
        if not st or st.get("status") != "holding":
            return
        close_t = _parse_hm(settings.leader_close_time, (14, 55))
        try:
            quote = self.broker.get_quote(code, priority=True)
        except Exception as e:
            logger.warning("leader_trader: {} 현재가 조회 실패 — {}", self._disp(code), e)
            return
        price = quote.price
        # tp 는 진입 시점 값으로 state 에 저장돼있지만, 보유 중 .env 핫리로드로
        # leader_tp_pct 가 바뀌면 이미 진입한 포지션에도 즉시 반영되도록 매 틱
        # entry 기준으로 재계산한다(2026-08-15 — 예전엔 진입 시 고정값이라
        # 보유 중 파라미터 변경이 적용 안 되는 버그가 있었음).
        tp_px = float(st["entry"]) * (1 + settings.leader_tp_pct / 100)
        mode = settings.leader_exit_mode

        reason = None
        state_dirty = False
        if mode == "split":
            entry = float(st["entry"])
            tp1_px = entry * (1 + settings.leader_split_tp1_pct / 100)
            tp2_px = entry * (1 + settings.leader_split_tp2_pct / 100)
            if not st.get("split_done"):
                if price >= tp1_px:
                    total_qty = int(st["qty"])
                    if total_qty > 1:
                        sell_qty = int(total_qty * settings.leader_split_tp1_ratio / 100 + 0.5)
                        sell_qty = max(1, min(sell_qty, total_qty - 1))
                    else:
                        sell_qty = 0
                    if sell_qty > 0:
                        self._partial_exit(st, code, sell_qty, price, now, tp1_px)
                        return
                    # 1주뿐이라 분할 불가 → 1차 목표에서 그냥 전량 청산
                    reason = f"+{settings.leader_split_tp1_pct:g}%익절"
                elif price <= st["stop"]:
                    reason = "손절"
                elif (now.hour, now.minute) >= close_t:
                    reason = "마감청산"
            else:
                if price >= tp2_px:
                    reason = f"+{settings.leader_split_tp2_pct:g}%2차익절"
                elif price <= st["stop"]:
                    reason = "본전확보청산"
                elif (now.hour, now.minute) >= close_t:
                    reason = "마감청산"
        elif mode == "trail":
            # 고점 갱신 → stop 은 고점 추종으로만 올라가고 절대 내려가지 않는다.
            peak = max(float(st.get("peak", st["entry"])), price)
            if peak != st.get("peak"):
                st["peak"] = peak
                state_dirty = True
            activate_px = float(st["entry"]) * (1 + settings.leader_trail_activate_pct / 100)
            activated = peak >= activate_px
            if activated:
                trail_stop = peak * (1 - settings.leader_trail_gap_pct / 100)
                if trail_stop > float(st["stop"]):
                    st["stop"] = trail_stop
                    state_dirty = True
            if price <= st["stop"]:
                reason = "트레일링청산" if activated else "손절"
            elif (now.hour, now.minute) >= close_t:
                reason = "마감청산"
        else:
            if price <= st["stop"]:
                reason = "손절"
            elif price >= tp_px:
                reason = f"+{settings.leader_tp_pct:g}%익절"
            elif (now.hour, now.minute) >= close_t:
                reason = "마감청산"
        if reason is None:
            if state_dirty:  # peak/stop 갱신이 있었을 때만 저장(불필요한 초단위 I/O 방지)
                self._save_state()
            return

        qty = int(st["qty"])
        # 관전 가상 포지션: 실주문 없이 가상 청산 (장중 매매 on 전환에도 가상 유지)
        if st.get("virtual"):
            entry = float(st["entry"])
            net = (price * (1 - _SELL_COMM) / (entry * (1 + _BUY_COMM)) - 1) * 100
            st.update({
                "status": "done", "exit": price,
                "exit_at": f"{now:%H:%M:%S}", "exit_reason": reason,
                "net_pct": round(net, 2),
            })
            self._save_state()
            notify(
                f"👁 **대장주봇 관전 — 가상 {reason}** {st.get('name', '')}({code}) "
                f"x{qty} @ {price:,.0f}\n"
                f"진입 {entry:,.0f} → net {net:+.2f}% (실주문 없음)"
            )
            logger.info(
                "leader_trader: [관전] 가상 청산 {} [{}] @ {:,.0f} net {:+.2f}%",
                self._disp(code), reason, price, net,
            )
            return
        try:
            resp = self.broker.place_order(code, "sell", qty, order_type="market")
        except OrderRejectedError as e:
            self._on_sell_reject(st, code, qty, price, now, reason, e)
            return  # 종료 처리됐거나(잔고 0) 다음 틱 재시도
        entry = float(st["entry"])
        src = st.get("src", "pullback")
        strategy = "leader_vwap_touch" if src == "vwap" else "leader_pullback"
        entry_label = "VWAP" if src == "vwap" else "눌림목"

        filled, avg_px = self._confirm_sell_fill(code, resp, qty)
        if filled <= 0:
            # 체결 0 — 여기서 done 으로 닫으면 손절선도 감시도 없는 물량이
            # 계좌에 그대로 남는다. 상태를 유지해 다음 틱에 다시 판다.
            # 매 틱 반복될 수 있으므로 알림은 억제 경유(_notify_reject).
            self._notify_reject(
                code,
                f"⚠️ **대장주봇 {reason} 미체결** {st.get('name', '')}({code}) "
                f"x{qty} @ {price:,.0f} — 체결 0주, 다음 틱에 재시도합니다.",
            )
            logger.warning("leader_trader: 청산 미체결 {} [{}] x{} — 재시도",
                           self._disp(code), reason, qty)
            return
        self._sell_reject.pop(code, None)  # 실제 체결 확인 → 거부 억제 해제
        if filled < qty:
            # 일부만 팔렸다. 판 만큼만 기록하고 잔량은 계속 들고 감시한다
            # (done 으로 닫지 않는다) → 다음 틱에 남은 수량으로 재매도.
            remain = qty - filled
            px = avg_px if avg_px > 0 else price
            st["qty"] = remain
            self._save_state()
            record_trade(
                symbol=code, side="sell", quantity=filled, price=px,
                reason=f"{entry_label} {reason}(부분체결)",
                broker_response=json.dumps(resp, ensure_ascii=False)[:500],
                strategy=strategy,
                details={"entry": entry, "exit_reason": reason, "remain_qty": remain},
            )
            notify(
                f"⚠️ **대장주봇 {reason} 부분체결** {st.get('name', '')}({code}) "
                f"주문 {qty}주 → 실제 {filled}주 @ {px:,.0f} — 잔량 {remain}주는 계속 보유·다음 틱 재매도"
            )
            logger.warning(
                "leader_trader: 청산 부분체결 {} [{}] {}주/{}주 — 잔량 {} 재시도",
                self._disp(code), reason, filled, qty, remain,
            )
            return
        if avg_px > 0:
            price = avg_px  # 손익률·기록은 실제 체결단가 기준

        net = (price * (1 - _SELL_COMM) / (entry * (1 + _BUY_COMM)) - 1) * 100
        st.update({
            "status": "done", "exit": price,
            "exit_at": f"{now:%H:%M:%S}", "exit_reason": reason, "net_pct": round(net, 2),
        })
        self._save_state()
        # 청산 완료 — 점유 해제(스톡봇이 이 종목을 다시 판단할 수 있게).
        if settings.leader_own_symbol_priority:
            position_owner.release(code, "leader")
        record_trade(
            symbol=code, side="sell", quantity=qty, price=price,
            reason=f"{entry_label} {reason}",
            broker_response=json.dumps(resp, ensure_ascii=False)[:500],
            strategy=strategy,
            details={"entry": entry, "net_pct": round(net, 2), "exit_reason": reason},
        )
        emoji = "🔴" if net < 0 else "🟢"
        notify(
            f"{emoji} **대장주봇 {reason}** {st.get('name', '')}({code}) x{qty} @ {price:,.0f}\n"
            f"진입 {entry:,.0f} → net {net:+.2f}%"
        )
        logger.info(
            "leader_trader: 청산 {} [{}] @ {:,.0f} net {:+.2f}%", self._disp(code), reason, price, net,
        )
