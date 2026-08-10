"""대장주 눌림목 전략 실전 모듈 (backtest_leader_pullback.py 의 라이브 구현).

흐름 (백테스트 06-09~06-12 확정 설정과 동일):
  · 대상   : 당일 data/leader_picks/날짜.json 의 1등 섹터 top3 바스켓
             — 바스켓 60%룰: 2·3등 stock_score ≥ 1등 × leader_band_ratio 일 때만 편입
             — 기존 앙상블 전략 종목(settings.symbols)과 겹치면 제외 (자본·포지션 충돌 방지)
  · 고점   : 9:00~선별시각 최고가(pre_high), floor = pre_high × (1 - 눌림한도)
  · 진입   : leader_interval_min 분봉에서 W=leader_w 스윙저점 확정봉 → 시장가 매수
             — 마지막 확정봉만 평가 (재시작 후 스테일 신호 추격 방지)
             — 같은 봉 동시 신호 시 순위 우선(1등>2등>3등), 하루 1종목 1회
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
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

from loguru import logger

from stock_bot.broker import KISBroker
from stock_bot.broker.kis import OrderRejectedError
from stock_bot.config import settings
from stock_bot.live import chart_snapshot
from stock_bot.live import position_owner
from stock_bot.market_calendar import KST as _KST
from stock_bot.names import get_name
from stock_bot.notify import notify
from stock_bot.storage import record_trade

# 백테스트와 동일 수수료 (검증·로그용 net 계산)
_BUY_COMM = 0.00015
_SELL_COMM = 0.00195

_ROOT = Path(__file__).resolve().parents[2]
_PICKS_DIR = _ROOT / "data" / "leader_picks"
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


class LeaderTrader:
    """하루 단위 상태머신: watching → holding → done."""

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
        """sector_baskets 전체를 합쳐 중복 제거한 단일 basket 반환 (rank 순 유지)."""
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
        if self._basket and self._state.get("status") == "watching":
            lead_sc = float(top3[0].get("stock_score", 0) or 0)
            thresh_sc = lead_sc * settings.leader_band_ratio
            logger.info(
                "leader_trader: {} 바스켓 {} [섹터 {}] (선별 {:02d}:{:02d}, {:.0f}%룰 stock_score 기준 {:.3f})",
                date,
                ", ".join(f"{m.get('name', '')}({m['code']})" for m in self._basket),
                self._active_sector_name or "1등",
                *self._trade_start, settings.leader_band_ratio * 100, thresh_sc,
            )

    def _save_state(self) -> None:
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
        watching(미보유·미진입)에서만 호출.
        """
        try:
            uh, um = (int(x) for x in settings.leader_switch_until.split(":")[:2])
        except Exception:
            uh, um = 13, 0
        if (now.hour, now.minute) > (uh, um):
            return
        iv = max(5, settings.leader_switch_interval_min)
        last = self._state.get("last_switch_eval")
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < iv * 60:
                    return
            except Exception:
                pass
        reval_path = _PICKS_DIR / f"{self._date}_reval.json"
        rev, _ = self._read_leaders(reval_path)
        if not rev:
            return
        self._state["last_switch_eval"] = now.isoformat()

        # §4-3 통합 재정렬 — 보유+신규 통합 점수정렬 → 상위 max_sectors 유지.
        self._reval_resort(rev, now)

    def _reval_resort(self, rev: list[dict[str, Any]], now: datetime) -> None:
        """§4-3 통합 재정렬: 보유 섹터 + 신규 섹터를 sector_score 순 정렬 후
        상위 leader_max_sectors 개만 감시로 유지.

        · 보유 섹터 점수는 reval 최신값으로 재계산(reval 에 없으면 0 → 탈락 후보).
        · 신규는 1등 sector_score 대비 leader_band_ratio(=0.6) 이상만 후보.
        · 상위 밖으로 밀린 보유 섹터 → 차트전용(_chart_only_sectors)으로 이동.
        watching(미보유·미진입)에서만 호출. 결과가 바뀔 때만 상태 저장·알림.
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

        before = set(self._sector_baskets)
        # 탈락(보유였는데 상위 밖) → 차트전용 이동.
        for s in list(self._sector_baskets):
            if s not in keep_set:
                self._chart_only_sectors[s] = self._sector_baskets.pop(s)
                self._sector_start_times.pop(s, None)
        # 신규/재진입(상위인데 미보유) → 바스켓 생성(차트전용에 있던 것도 승격).
        for s in keep:
            if s in self._sector_baskets:
                continue
            L = rev_by_sector.get(s)
            if not L:
                continue
            nb = self._build_basket(self._top3_of(L))
            if nb:
                self._sector_baskets[s] = nb
                self._sector_start_times[s] = (now.hour, now.minute)
                self._chart_only_sectors.pop(s, None)

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
        # 매매 off 여도 리턴하지 않는다 — 관전 모드로 신호 판정·가상매매까지 수행
        now = datetime.now(tz=_KST)
        date = f"{now:%Y-%m-%d}"
        if date != self._date:
            self._load_day(date)

        status = self._state.get("status", "watching")
        # 점유 원장 정합 — 보유 종목은 confirmed, 청산·스테일 점유는 청소.
        # 가상 보유(관전)는 실제 점유가 아니므로 원장에 올리지 않는다.
        if settings.leader_own_symbol_priority:
            held = (
                [self._state["symbol"]]
                if status == "holding" and self._state.get("symbol")
                and not self._state.get("virtual")
                else []
            )
            position_owner.reconcile("leader", held)
        # 보유·완료 상태에선 _scan_entries 가 안 돌아 차트 스냅샷이 멈춘다 →
        # 여기서 바스켓+보유 종목 차트를 계속 떨궈 차트 탭이 얼지 않게 한다(표시 전용).
        if status in ("holding", "done"):
            self._refresh_charts()
        if status == "done":
            return
        if status == "holding":
            self._manage_position(now)
            return

        # watching
        if not (_PICKS_DIR / f"{date}.json").exists():
            if self._no_picks_logged != date and now.time() >= dtime(9, 40):
                logger.info("leader_trader: {} picks 미생성 — 선별 대기", date)
                self._no_picks_logged = date
            return
        if not self._basket:
            self._load_day(date)  # picks 가 틱 사이에 생성된 경우 재로드
            if not self._basket:
                return
        # 섹터 전환(관전 실험): 미보유·미진입(watching) 상태에서만 주기 재평가.
        if settings.leader_switch_enabled:
            self._maybe_switch(now)
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
        sym = self._state.get("symbol")
        if sym:
            codes.add(_bare(sym))
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

        # 코드별 섹터 start_time 매핑 (섹터 추가 시각이 다를 수 있음)
        code_start: dict[str, tuple[int, int]] = {}
        for s_name, s_basket in self._sector_baskets.items():
            s_start = self._sector_start_times.get(s_name, self._trade_start)
            for m in s_basket:
                code_start.setdefault(_bare(m["code"]), s_start)

        for m in self._basket:  # rank 순 → 동시 신호 시 순위 우선
            code = _bare(m["code"])
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
                return  # 하루 1종목 — 진입 성공 시 종료

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
            "bar_time": times[j],
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
        진입가=이번봉 종가, 참조저점=이번봉 저가, 손절=참조×(1-stop_buf%),
        익절=진입가×(1+tp%). 스윙저점·전고점 floor·회복확인(직전고가 돌파)은 미사용.
        장대양봉컷·상한가컷·하루1종목·마감청산은 pullback 과 동일.
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
            "price_now": quote.price, "bar_time": times[j],
        }

    def _enter(
        self, member: dict[str, Any], code: str, sig: dict[str, Any], now: datetime
    ) -> bool:
        price = sig["price_now"] or sig["entry_est"]
        if not price or price <= 0:
            logger.warning(
                "leader_trader: {} 진입 스킵 — 유효 가격 없음 (price_now={}, entry_est={})",
                self._disp(code), sig.get("price_now"), sig.get("entry_est"),
            )
            self._state.setdefault("skipped", {})[code] = "가격 0/None"
            self._save_state()
            return False
        qty = int(settings.leader_budget_krw // price)
        if qty < 1:
            logger.warning(
                "leader_trader: {} 예산 부족 (예산 {:,.0f} < 현재가 {:,.0f})",
                self._disp(code), settings.leader_budget_krw, price,
            )
            self._state.setdefault("skipped", {})[code] = "예산 부족"
            self._save_state()
            return False
        # 관전(매매 off): 실주문 없이 가상 진입 — 상태머신·청산 판정은 실전과 동일.
        # 점유원장·record_trade 는 건드리지 않는다(진짜 자본/DB 오염 방지).
        if not settings.leader_trade_enabled:
            entry = price
            tp_px = entry * (1 + settings.leader_tp_pct / 100)
            self._state.update({
                "status": "holding", "virtual": True,
                "symbol": code, "name": member.get("name", ""),
                "rank": member.get("rank", 1), "qty": qty,
                "entry": entry, "ref": sig["ref"], "stop": sig["stop"], "tp": tp_px,
                "entry_at": f"{now:%H:%M:%S}", "bar_time": sig["bar_time"],
            })
            self._save_state()
            notify(
                f"👁 **대장주봇 관전 — 가상매수** {member.get('name', '')}({code}) "
                f"x{qty} @ {entry:,.0f}\n"
                f"스윙저점 {sig['ref']:,.0f} · 손절 {sig['stop']:,.0f} · "
                f"목표 {tp_px:,.0f} (+{settings.leader_tp_pct:g}%) — 실주문 없음"
            )
            logger.info(
                "leader_trader: [관전] 가상 진입 {} x{} @ {:,.0f} (확정봉 {} / stop {:,.0f} / tp {:,.0f})",
                self._disp(code), qty, entry, sig["bar_time"][:4], sig["stop"], tp_px,
            )
            return True
        # 점유 선점: 스톡봇이 같은 종목을 이미 잡고 있으면 양보(더블 매수 방지).
        if settings.leader_own_symbol_priority and not position_owner.claim(code, "leader", qty):
            logger.info("leader_trader: {} [점유-양보] 스톡봇이 선점 → 진입 skip", self._disp(code))
            self._state.setdefault("skipped", {})[code] = "스톡봇 선점"
            self._save_state()
            return False
        try:
            resp = self.broker.place_order(code, "buy", qty, order_type="market")
        except OrderRejectedError as e:
            if settings.leader_own_symbol_priority:
                position_owner.release(code, "leader")  # 미체결 점유 회수
            notify(f"🚫 **대장주봇 매수 거부** {member.get('name', '')}({code}) x{qty}: {e}")
            self._state.setdefault("skipped", {})[code] = f"주문 거부: {e}"
            self._save_state()
            return False

        entry = price  # 시장가 — 현재가 기준 (체결가는 broker_response 참고)
        tp_px = entry * (1 + settings.leader_tp_pct / 100)
        self._state.update({
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
        })
        self._save_state()
        record_trade(
            symbol=code, side="buy", quantity=qty, price=entry,
            reason=f"눌림목 진입 (스윙저점 {sig['ref']:,.0f}, 확정봉 {sig['bar_time'][:4]})",
            broker_response=json.dumps(resp, ensure_ascii=False)[:500],
            strategy="leader_pullback",
            details={"ref": sig["ref"], "stop": sig["stop"], "tp": tp_px,
                     "pre_high": sig["pre_high"], "rank": member.get("rank", 1)},
        )
        notify(
            f"🟢 **대장주봇 매수** {member.get('name', '')}({code}) x{qty} @ {entry:,.0f}\n"
            f"스윙저점 {sig['ref']:,.0f} · 손절 {sig['stop']:,.0f} · "
            f"목표 {tp_px:,.0f} (+{settings.leader_tp_pct:g}%)"
        )
        logger.info(
            "leader_trader: 진입 {} x{} @ {:,.0f} (stop {:,.0f} / tp {:,.0f})",
            self._disp(code), qty, entry, sig["stop"], tp_px,
        )
        return True

    # ── 보유 관리 ────────────────────────────────────────────────────
    def _manage_position(self, now: datetime) -> None:
        st = self._state
        code = st["symbol"]
        close_t = _parse_hm(settings.leader_close_time, (14, 55))
        try:
            quote = self.broker.get_quote(code)
        except Exception as e:
            logger.warning("leader_trader: {} 현재가 조회 실패 — {}", self._disp(code), e)
            return
        price = quote.price

        reason = None
        if price <= st["stop"]:
            reason = "손절"
        elif price >= st["tp"]:
            reason = f"+{settings.leader_tp_pct:g}%익절"
        elif (now.hour, now.minute) >= close_t:
            reason = "마감청산"
        if reason is None:
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
            notify(f"🚫 **대장주봇 매도 거부** {st.get('name', '')}({code}) x{qty}: {e}")
            logger.error("leader_trader: 매도 거부 {} — {}", self._disp(code), e)
            return  # 다음 틱 재시도

        entry = float(st["entry"])
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
            reason=f"눌림목 {reason}",
            broker_response=json.dumps(resp, ensure_ascii=False)[:500],
            strategy="leader_pullback",
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
