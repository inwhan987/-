# -*- coding: utf-8 -*-
"""장중 루프 (명세 2절·6~11절). WS → 봉 → 트리거 → 주문/기록 전 경로.

08:45  감시 리스트 로드(전일 야간 스캔 결과) + 보유 포지션 복구 + 레짐 판정
09:00~ WS 구독(신규 n_new + 보유). 틱 → 손절/익절 즉시, 3분봉 → 트리거/트레일링
       트리거 = 종합점수(total_score) 하한 미만이면 '점수보류'. 넘으면 entry_batch_sec 동안 모았다가
       종합점수 높은 순으로 진입(같은 봉에서 여러 종목이 동시에 걸릴 때 먼저 온 순이 아니라 점수 순).
15:20  타임스톱·추세이탈 → 청산. 미트리거 종목의 사유·당일 OHLC 를 signals 에 기록
15:31  WS 종료

세 모드 공통. 주문은 orders.place(mode) 에서만 갈린다.
장중 REST 는 주문·체결조회·끊긴 구간 백필 뿐이다(명세 10절).
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime, timedelta

from loguru import logger

from stock_bot.broker.kis import KISBroker
from stock_bot.broker.kis_ws import MAX_SUBSCRIBE, Bar, SwingTickStream, Tick

from . import exits, ledger, orders, regime, state, store, triggers
from .collector import IDX_CODE
from .config import SwingCfg, cfg, shared_slots_now, trade_enabled_now
from .levels import entry_levels

EOD_TIME = "152000"        # 타임스톱·추세이탈 판정
STOP_TIME = "153100"       # WS 종료
_NOFILL_MAX = 2            # 같은 종목 미체결 반복 → 감시 제외


_TIER_KO = {"priority": "우선★", "normal": "일반"}


def _hms() -> str:
    return time.strftime("%H%M%S")


def _notify(msg: str) -> None:
    """디스코드 — 다른 봇(🤖 스톡봇 / 👑 대장주봇)과 같은 채널이라 본문 헤더로 구분."""
    try:
        from stock_bot.notify.discord import notify
        notify(f"📈 스윙봇\n{msg}")
    except Exception as e:  # noqa: BLE001
        logger.debug("notify 실패: {}", e)


def _tp_str(tp) -> str:
    """익절가 표시 — 익절 없음(None)은 '없음'."""
    return f"{float(tp):,.0f}" if tp else "없음"


def _name(code: str) -> str:
    """'005930(삼성전자)' — meta 에 없으면 코드만."""
    try:
        r = store.conn().execute("SELECT name FROM meta WHERE code=?", (code,)).fetchone()
        return f"{code}({r[0]})" if r and r[0] else code
    except Exception:  # noqa: BLE001
        return code


class SwingLive:
    def __init__(self, c: SwingCfg | None = None, trade_date: str | None = None):
        self.c = c or cfg()
        self.mode = self.c.mode
        # 로그·디스코드 접두 — 모의(paper)는 표시 안 함, dryrun/real 만 [mode] 로 구분
        self.tag = "" if self.mode == "paper" else f"[{self.mode}] "
        self.trade_date = trade_date or datetime.now().strftime("%Y%m%d")
        self.wl_date: str | None = None
        self.watch: dict[str, dict] = {}          # code → watchlist 행 (신규 감시)
        self._reserve: list[dict] = []            # 감시 예비 후보 (보유·탈락으로 자리가 비면 승격)
        self.holdings: dict[str, dict] = {}       # code → positions 행 (HOLDING)
        self.session: dict[str, dict] = {}        # code → {open, session_high, session_low, last, bars}
        self.last_reason: dict[str, str] = {}     # code → 마지막 미트리거 사유
        self.nofill: dict[str, int] = {}
        self.regime_ok, self.size_mult = True, 1.0
        self.new_today = 0
        self.new_normal_today = 0                 # 오늘 일반 등급으로 체결된 수 (일반 등급 하루 한도용)
        self._priority: set[str] = set()          # 우선 등급 종목 (종합 ≥ priority_score, 없으면 감시 순위 1~N)
        self._confirm: dict[str, tuple] = {}      # 일반 등급 확인 대기: code → (reason, score, hms, 남은 봉 수)
        self.eod_done = False
        self.stream: SwingTickStream | None = None
        self._broker: KISBroker | None = None
        self._pending_order: set[str] = set()
        self._armed: dict[str, tuple] = {}        # code → (bar, lv, reason, score, hms) 모음 창 대기
        self._armed_task: asyncio.Task | None = None
        self._noted: set[str] = set()             # 같은 보류/차단 로그 하루 1회
        self._ticks = 0                           # 수신 틱 수 (로그 요약용)
        self._last_tick_at: float | None = None   # 마지막 틱 monotonic (무수신 경고용)
        self._backfill_task: asyncio.Task | None = None   # 끊김 백필 (재접속을 막지 않게 백그라운드)
        self._gap_at: float | None = None          # 끊김 시각(monotonic) — 다음 틱에서 실제 공백 길이 산출
        self._gap_len: float = 0.0
        self._silent_warned_at: float | None = None

    # ── 준비 ─────────────────────────────────────────────────────
    @property
    def broker(self) -> KISBroker:
        if self._broker is None:
            self._broker = KISBroker()
        return self._broker

    def prepare(self) -> list[str]:
        """감시 리스트·포지션·레짐 → 구독 코드. 반환: 구독할 코드."""
        store.init_db()
        self._recover_positions()
        ledger.reconcile(self.mode, list(self.holdings))   # 전날 보유분 점유 등록·고아 청소
        self._load_watchlist()
        codes = list(self.holdings) + [cd for cd in self.watch if cd not in self.holdings]
        if len(codes) > MAX_SUBSCRIBE:
            over = codes[MAX_SUBSCRIBE:]
            for cd in over:
                self.watch.pop(cd, None)
                self._log_no_trigger(cd, state.DROP_NOSLOT)
            codes = codes[:MAX_SUBSCRIBE]
        if self.wl_date and self.watch:
            store.set_subscribed(self.wl_date, list(self.watch), 1)
        logger.info("{}진입 규칙: 창 {}~{} 종합점수 하한 {:.0f} 모음 창 {}초 하루 상한 {} · "
                    "우선 등급 종합≥{:.0f}(없으면 순위 1~{}) 즉시 {}종목 · 일반 등급 다음 {}봉 유지 확인, 하루 {}건 · "
                    "오늘 체결 {} (일반 {})",
                    self.tag, self.c.entry_from, self.c.entry_until, self.c.entry_min_score,
                    self.c.entry_batch_sec, self.c.max_new_per_day, self.c.priority_score,
                    self.c.priority_fallback_rank, len(self._priority), self.c.normal_confirm_bars,
                    self._normal_cap(), self.new_today, self.new_normal_today)
        logger.info("{}{} 준비: 보유 {} 신규감시 {} (wl={}) 레짐={} mult={}",
                    self.tag, self.trade_date, len(self.holdings), len(self.watch),
                    self.wl_date, self.regime_ok, self.size_mult)
        return codes

    def _recover_positions(self) -> None:
        """크래시·재시작 복구 (명세 11절). HOLDING 은 그대로, 전날 ARMED/ENTERED 는 정리."""
        acct = {}
        if self.mode != "dryrun":
            try:
                acct = orders.broker_positions(self.mode, self.broker)
            except Exception as e:  # noqa: BLE001
                logger.error("잔고 조회 실패 — ENTERED 정리 보류: {}", e)
                acct = None
        for p in store.open_positions(self.mode):
            st = p["state"]
            if st == state.HOLDING:
                self.holdings[p["code"]] = p
            elif st == state.ARMED:
                state.drop(p, state.DROP_NOFILL, "재시작 시 ARMED 잔존")
            elif st == state.ENTERED:
                if acct is None:
                    self.holdings[p["code"]] = p       # 판단 보류 — 보유로 간주해 손절은 돌린다
                    continue
                q = acct.get(p["code"], 0) if self.mode != "dryrun" else int(p.get("shares") or 0)
                if q > 0 and p.get("entry_px"):
                    sp, tp = entry_levels(float(p["entry_px"]), self.c.exit_rule(p.get("strategy")))
                    state.holding(p, q, float(p["entry_px"]), sp, tp)
                    self.holdings[p["code"]] = p
                else:
                    state.drop(p, state.DROP_NOFILL, "재시작 시 잔고 없음")
        today_new = [p for p in store.load_positions(self.mode)
                     if p.get("entry_date") == self.trade_date
                     and p.get("state") in (state.ENTERED, state.HOLDING, state.EXIT)]
        self.new_today = len(today_new)
        self.new_normal_today = sum(1 for p in today_new if "tier=normal" in str(p.get("note") or ""))
        for cd, p in self.holdings.items():
            logger.info("{}보유 복구 {} {} {}주 @{:,.0f} 손절 {:,.0f} 익절 {} (진입 {}) 규칙 {}", self.tag, cd,
                        p.get("strategy"), int(p.get("shares") or 0), float(p.get("entry_px") or 0),
                        float(p.get("stop_px") or 0), _tp_str(p.get("tp_px")), p.get("entry_date"),
                        self.c.exit_rule(p.get("strategy")).label())

    def _load_watchlist(self) -> None:
        """전일 야간 스캔 결과. 실패한 날·오래된 리스트는 재사용하지 않는다(명세 11절)."""
        wl_date = store.latest_watchlist_date()
        if not wl_date:
            logger.warning("감시 리스트 없음 — 신규 진입 없음")
            return
        last_daily = store.conn().execute(
            "SELECT MAX(date) FROM daily WHERE code=?", (IDX_CODE,)).fetchone()[0]
        if wl_date >= self.trade_date or (last_daily and wl_date < last_daily):
            logger.warning("감시 리스트 날짜 {} 가 오늘({})/최근 일봉({}) 과 안 맞음 — 신규 진입 없음",
                           wl_date, self.trade_date, last_daily)
            return
        run = store.get_run(wl_date, "nightly")
        if run is None or run.get("status") != "ok":
            logger.warning("야간 배치 {} 상태 {} — 감시 리스트 재사용 안 함",
                           wl_date, run and run.get("status"))
            return
        self.wl_date = wl_date
        self.regime_ok, self.size_mult = regime.market_ok(wl_date, self.c)
        rows = store.load_watchlist(wl_date)
        n_new = self._watch_cap()
        picked = 0
        for r in rows:
            cd = r["code"]
            if cd in self.holdings or cd in self.watch:
                continue                      # 보유 중이면 감시 자리를 쓰지 않는다 — 다음 순위가 그만큼 채워진다
            if picked >= n_new:
                self._reserve.append(r)       # 예비 — 장중 감시 자리가 비면 승격(eod 까지 미승격이면 슬롯없음 기록)
                continue
            self.watch[cd] = r
            picked += 1
        self._priority = self._pick_priority()
        logger.info("{}감시 리스트 {} 적재: 후보 {} → 감시 {} (슬롯 {}) 상위: {}", self.tag, wl_date,
                    len(rows), len(self.watch), n_new,
                    ", ".join(f"{cd} {self._score(r):.0f}" for cd, r in list(self.watch.items())[:5]))
        logger.info("{}우선 등급 {}종목 ({}): {}", self.tag, len(self._priority),
                    f"종합≥{self.c.priority_score:.0f}" if any(self._score(self.watch[cd]) >= self.c.priority_score
                                                              for cd in self._priority)
                    else f"종합≥{self.c.priority_score:.0f} 없음 → 순위 1~{self.c.priority_fallback_rank}",
                    ", ".join(f"{cd} {self._score(self.watch[cd]):.0f}" for cd in self._priority) or "-")
        if not self.regime_ok and self.size_mult <= 0:
            logger.warning("레짐 차단(지수 < MA{}) — 오늘 신규 진입 없음, 감시는 기록용으로 유지",
                           self.c.regime_ma)
            for cd, r in self.watch.items():
                self._log_no_trigger(cd, "레짐차단", strategy=r["strategy"])

    # ── 신호 기록 ─────────────────────────────────────────────────
    def _log_no_trigger(self, code: str, reason: str, strategy: str | None = None) -> None:
        if not self.wl_date:
            return
        st = strategy or (self.watch.get(code) or {}).get("strategy")
        if not st:
            return
        store.log_signal({"date": self.wl_date, "code": code, "strategy": st,
                          "no_trigger_reason": reason, "trade_date": self.trade_date})

    def _sess(self, t: Tick) -> dict:
        s = self.session.get(t.code)
        if s is None:
            s = self.session[t.code] = {"open": t.day_open or t.price, "session_high": t.day_high or t.price,
                                        "session_low": t.day_low or t.price, "last": t.price, "bars": 0}
        s["session_high"] = max(s["session_high"], t.day_high or t.price, t.price)
        s["session_low"] = min(s["session_low"], t.day_low or t.price, t.price) if s["session_low"] else t.price
        s["last"] = t.price
        return s

    # ── 콜백 ─────────────────────────────────────────────────────
    async def on_tick(self, t: Tick) -> None:
        if self._last_tick_at is None:
            logger.info("{}첫 틱 수신 {} @{:,.0f} — WS 데이터 흐름 시작", self.tag, t.code, t.price)
        self._last_tick_at = time.monotonic()
        if self._gap_at is not None:                # 끊김 뒤 첫 틱 → 실제 공백 길이
            self._gap_len = self._last_tick_at - self._gap_at
            self._gap_at = None
            logger.info("{}WS 복구 — 틱 공백 {:.0f}초", self.tag, self._gap_len)
        self._ticks += 1
        self._sess(t)
        pos = self.holdings.get(t.code)
        if pos and t.code not in self._pending_order:
            r = exits.check_tick(pos, t.price, self.c.exit_rule(pos.get("strategy")))
            if r:
                await self._exit(pos, r[1], r[2])

    async def on_store_bar(self, b: Bar) -> None:
        store.save_bar(b, self.trade_date)

    async def on_bar(self, b: Bar) -> None:
        s = self.session.get(b.code)
        if s is None:
            return
        s["bars"] += 1
        pos = self.holdings.get(b.code)
        if pos:
            if b.code in self._pending_order:
                return
            r = exits.check_bar(pos, b, self.c.exit_rule(pos.get("strategy")))
            store.upsert_position(pos)                 # peak/trough/trail_on 갱신
            if r:
                await self._exit(pos, r[1], r[2])
            return
        lv = self.watch.get(b.code)
        if lv is None:
            return
        await self._maybe_enter(b, lv, s)

    async def on_gap(self, gap_sec: float, codes: list[str]) -> None:
        """WS 끊김 콜백. 끊긴 구간은 복구 불가 → REST 1분봉으로 세션 고저·peak 백필 (명세 11절).

        KIS 모의 WS 는 매시 정각에 서버가 연결을 끊는다(9/18 실측: 09~15시 :00:01 마다 'no close frame').
        여기서 백필을 await 하면 33종목 REST 에 ~5분이 걸려 그동안 재접속이 막혔다(14:00:01 끊김 →
        14:05:57 재접속). 그래서 백필은 백그라운드 태스크로 넘기고 즉시 돌아가 스트림이 바로 재접속하게 한다.
        gap_sec 는 스트림이 준 값(접속 이후 경과라 부정확) — 알림엔 마지막 틱 기준 실제 공백을 쓴다."""
        if _hms() < "090000":
            # 장 전(08:50 기동~개장) 끊김은 놓친 봉이 없다 — 백필·알림 없이 재접속만 (스트림이 알아서 한다)
            logger.info("{}장 전 WS 끊김 — 개장 전이라 백필 생략", self.tag)
            return
        if self._backfill_task and not self._backfill_task.done():
            logger.info("{}WS 끊김 — 백필 진행 중이라 추가 백필 생략", self.tag)
            return
        logger.warning("{}WS 끊김 — 재접속 먼저, REST 백필 {}종목은 백그라운드", self.tag, len(codes))
        self._gap_at = time.monotonic()
        self._gap_len = 0.0
        self._backfill_task = asyncio.create_task(self._backfill(list(codes)))

    async def _backfill(self, codes: list[str]) -> None:
        # 재접속(백오프 최대 30초)이 끝난 뒤 받아야 끊긴 구간 전체가 REST 1분봉에 들어온다
        await asyncio.sleep(45)
        targets = [cd for cd in codes if cd in self.holdings] + \
                  [cd for cd in codes if cd not in self.holdings]
        n_ok = n_fail = 0
        for cd in targets:
            try:
                rows = await asyncio.to_thread(self.broker.get_minute_ohlcv, cd, 1, 30)
            except Exception as e:  # noqa: BLE001
                logger.debug("백필 실패 {}: {}", cd, e)
                n_fail += 1
                continue
            if not rows:
                n_fail += 1
                continue
            n_ok += 1
            hi = max(r["high"] for r in rows)
            lo = min(r["low"] for r in rows)
            s = self.session.setdefault(cd, {"open": rows[-1]["open"], "session_high": hi,
                                             "session_low": lo, "last": rows[0]["close"], "bars": 0})
            s["session_high"] = max(s["session_high"], hi)
            s["session_low"] = min(s["session_low"], lo)
            pos = self.holdings.get(cd)
            if pos:
                pos["peak"] = max(float(pos.get("peak") or pos["entry_px"]), hi)
                pos["trough"] = min(float(pos.get("trough") or pos["entry_px"]), lo)
                store.upsert_position(pos)
        gap = f"틱 공백 {self._gap_len:.0f}초" if self._gap_len else "아직 복구 안 됨"
        logger.info("{}REST 백필 완료 {}/{}종목 (실패 {}) — {} · 세션 고저 갱신", self.tag, n_ok, len(targets),
                    n_fail, gap)
        # 매시 정각 끊김→재접속→백필 성공은 정상 동작이라 디스코드엔 안 올린다. 백필 실패가 있을 때만 알림.
        if n_fail:
            _notify(f"⚠️ {self.tag}WS 끊김({gap}) → REST 백필 실패 {n_fail}/{len(targets)}종목 (성공 {n_ok})"
                    f" · 재연결 {self.stream.reconnects if self.stream else '?'}회 · {self._ws_summary()}")

    # ── 진입 ─────────────────────────────────────────────────────
    def _watch_cap(self) -> int:
        """오늘 감시 자리 수. 보유가 watch_hold 를 넘으면 WS 한도(41) 만큼 줄인다."""
        return max(0, self.c.watch_new - max(0, len(self.holdings) - self.c.watch_hold))

    def _promote_reserve(self) -> None:
        """감시 자리가 빈 만큼 예비 후보를 승격하고 장중 구독을 추가한다.

        체결로 보유로 옮겨가거나 미체결·점유충돌로 빠진 자리를 다음 순위로 메운다.
        구독 한도에 걸리면 승격하지 않고 예비에 다시 넣는다(자리가 나면 다음 호출에서 승격).
        """
        cap = self._watch_cap()
        while self._reserve and len(self.watch) < cap:
            r = self._reserve.pop(0)
            cd = r["code"]
            if cd in self.holdings or cd in self.watch:
                continue
            if self.stream is not None:
                if len(self.stream.codes) >= MAX_SUBSCRIBE:
                    self._reserve.insert(0, r)
                    self._note("promote_full", "{}구독 한도 {} 도달 — 감시 승격 보류", self.tag, MAX_SUBSCRIBE)
                    return
                self.stream.subscribe(cd)
            self.watch[cd] = r
            logger.info("{}감시 승격 {} {} {} 종합 {:.0f} — 감시 {}/{} 구독 {}", self.tag, cd, _name(cd),
                        r.get("strategy"), self._score(r), len(self.watch), cap,
                        len(self.stream.codes) if self.stream else "-")
        self._priority = self._pick_priority()

    def _pick_priority(self) -> set[str]:
        """우선 등급 = 감시 중 종합 ≥ priority_score. 하나도 없으면 감시 순위 1~priority_fallback_rank.
        (감시 리스트는 순위순으로 적재되므로 적재 순서가 곧 순위. rank_overall 컬럼이 있으면 그걸 우선.)"""
        hi = {cd for cd, r in self.watch.items() if self._score(r) >= self.c.priority_score}
        if hi:
            return hi
        out: set[str] = set()
        for i, (cd, r) in enumerate(self.watch.items(), 1):
            try:
                rk = int(r.get("rank_overall") or i)
            except (TypeError, ValueError):
                rk = i
            if rk <= self.c.priority_fallback_rank:
                out.add(cd)
        return out

    def _tier(self, code: str) -> str:
        return "priority" if code in self._priority else "normal"

    def _normal_cap(self) -> int:
        """일반 등급 하루 신규 한도 = 하루 상한 × 비율(내림, 최소 1). 우선 등급은 하루 상한 전체를 쓴다."""
        return max(1, int(self.c.max_new_per_day * self.c.normal_max_ratio))

    def _entry_allowed(self, tier: str = "priority") -> str | None:
        now = _hms()
        if now < self.c.entry_from:
            return "시간전"
        if now > self.c.entry_until:
            return "시간후"
        if not trade_enabled_now(self.c.trade_enabled):
            return "매수OFF"                      # SWING_TRADE_ENABLED=false — 신규매수만 차단
        if not self.regime_ok and self.size_mult <= 0:
            return "레짐차단"
        if self._slots_used() >= self._shared()[0]:
            return state.DROP_NOSLOT                # 공용 슬롯(단타+스윙) 소진
        if self.new_today >= self.c.max_new_per_day:
            return "일일한도"
        if tier == "normal" and self.new_normal_today >= self._normal_cap():
            return "일반한도"                       # 나머지 자리는 우선 등급 몫 (안 채워지면 그대로 비움)
        return None

    def _shared(self) -> tuple[int, float]:
        """(공용 최대 슬롯, 1건 금액) — STOCK_MAX_POSITIONS·STOCK_BUDGET_KRW 장중 핫리드."""
        return shared_slots_now(self.c.max_positions, self.c.position_krw)

    def _slots_used(self) -> int:
        """공용 슬롯 사용 수. 원장(stock+swing 소유) 기준; dryrun 은 원장 미사용이라 가상 보유+주문중을 더한다.
        원장 조회 실패 시 내 보유+주문중만으로 판정(보수적 폴백 아님 — 스톡봇 몫을 못 보므로 로그만)."""
        mine = len(self.holdings) + len(self._pending_order)
        used = ledger.shared_used(self.mode)
        if used is None:
            return mine
        return used + mine if self.mode == "dryrun" else max(used, mine)

    def _size(self, px: float, lv: dict) -> tuple[int, str | None]:
        slot = self._shared()[1] * self.size_mult
        shares = int(slot // px)
        vma = lv.get("ref_value_ma20")
        if vma and self.c.max_order_share > 0:
            cap = int(self.c.max_order_share * float(vma) // px)
            if cap < shares:
                # 20일 평균 거래대금의 max_order_share(기본 1%) 를 넘지 않게 — 한산한 종목은 슬롯보다 작게 들어간다
                logger.info("{}주문규모 캡 {}: 슬롯 {:,.0f}원 → {}주 이지만 거래대금 {:,.0f}×{:.1%}={:,.0f}원 → {}주",
                            self.tag, lv.get("code") or "", slot, shares, float(vma), self.c.max_order_share,
                            self.c.max_order_share * float(vma), cap)
                shares = cap
        if shares <= 0:
            return 0, "주문규모"
        return shares, None

    @staticmethod
    def _score(lv: dict) -> float:
        """감시 행의 종합점수(total_score = 셋업 백분위 + 있는 축 평균). 구 리스트(컬럼 없음)는 셋업 백분위."""
        for k in ("total_score", "pscore"):
            v = lv.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _note(self, key: str, msg: str, *args) -> None:
        """같은 종목·같은 사유는 하루 한 번만 INFO (매 봉 반복 방지)."""
        if key not in self._noted:
            self._noted.add(key)
            logger.info(msg, *args)

    def _log_trigger(self, b: Bar, lv: dict, reason: str, block: str | None, hms: str | None = None) -> None:
        """트리거 기록 — 시각·가격은 남긴다 (명세 14절: 왜 안 샀나). block=None 이면 진입."""
        store.log_signal({"date": self.wl_date, "code": b.code, "strategy": lv["strategy"],
                          "trigger_time": hms or _hms(), "trigger_px": b.close, "trigger_reason": reason,
                          "no_trigger_reason": block, "trade_date": self.trade_date})

    async def _maybe_enter(self, b: Bar, lv: dict, s: dict) -> None:
        ok, reason = triggers.check(lv["strategy"], b, lv, s)
        pending = self._confirm.pop(b.code, None)    # 일반 등급 확인 대기 중이던 건 (이번 봉에서 판정)
        if not ok:
            if pending:
                self.last_reason[b.code] = f"{pending[0]}/다음봉미유지"
                self._log_trigger(b, lv, pending[0], "다음봉미유지", pending[2])
                logger.info("{}일반 등급 {} {} {} — 다음 봉 미유지({}) → 취소", self.tag, b.code,
                            lv["strategy"], pending[0], reason)
            else:
                self.last_reason[b.code] = reason
            return
        if b.code in self._armed:
            return                                   # 이미 모음 창에서 대기 중
        tier = self._tier(b.code)
        block = self._entry_allowed(tier)
        if block:
            self.last_reason[b.code] = f"{reason}/{block}"
            self._log_trigger(b, lv, reason, block)
            self._note(f"{b.code}/{block}", "{}트리거 감지 {} {} {} @{:,.0f} → 미진입: {}",
                       self.tag, b.code, lv["strategy"], reason, b.close, block)
            return
        sc = self._score(lv)
        if sc < self.c.entry_min_score:
            self.last_reason[b.code] = f"{reason}/점수보류"
            self._log_trigger(b, lv, reason, "점수보류")
            self._note(f"{b.code}/점수보류", "{}트리거 감지 {} {} {} @{:,.0f} 종합 {:.0f} < 하한 {:.0f} → 점수보류",
                       self.tag, b.code, lv["strategy"], reason, b.close, sc, self.c.entry_min_score)
            return
        if tier == "normal" and self.c.normal_confirm_bars > 0:
            left = (pending[3] if pending else self.c.normal_confirm_bars) - (1 if pending else 0)
            if left > 0:
                self._confirm[b.code] = (pending[0] if pending else reason, sc, pending[2] if pending else _hms(), left)
                logger.info("{}트리거 {} {} {} @{:,.0f} 종합 {:.0f} — 일반 등급(<{:.0f}) → 다음 {}봉 유지 확인 대기",
                            self.tag, b.code, lv["strategy"], reason, b.close, sc, self.c.priority_score, left)
                return
            reason = pending[0] if pending else reason
            logger.info("{}일반 등급 {} {} {} — 다음 봉 유지 확인 → 진입 진행", self.tag, b.code, lv["strategy"], reason)
        self._armed[b.code] = (b, lv, reason, sc, _hms(), tier)
        logger.info("{}트리거 {} {} {} @{:,.0f} 종합 {:.0f} {} — {}초 모음 창 대기 ({}건)",
                    self.tag, b.code, lv["strategy"], reason, b.close, sc, _TIER_KO[tier],
                    self.c.entry_batch_sec, len(self._armed))
        if self._armed_task is None or self._armed_task.done():
            self._armed_task = asyncio.create_task(self._flush_armed())

    async def _flush_armed(self) -> None:
        """모음 창이 닫히면 종합점수 높은 순으로 진입. 하루 상한·슬롯은 한 건씩 다시 확인."""
        await asyncio.sleep(max(0, self.c.entry_batch_sec))
        items = sorted(self._armed.values(), key=lambda x: (0 if x[5] == "priority" else 1, -x[3]))
        self._armed.clear()
        if not items:
            return
        _notify(f"{self.tag}트리거 {len(items)}건 → 진입 순서(우선 등급 → 종합점수순): "
                + " > ".join(f"{_name(b.code)} {sc:.0f}{'★' if tier == 'priority' else ''}"
                             for b, lv, reason, sc, hms, tier in items))
        logger.info("{}진입 순서(우선 등급 → 종합점수순): {}", self.tag,
                    " > ".join(f"{b.code} {sc:.0f} {_TIER_KO[tier]}" for b, _, _, sc, _, tier in items))
        for b, lv, reason, sc, hms, tier in items:
            if b.code not in self.watch or b.code in self.holdings:
                continue                             # 그 사이 감시 제외/보유
            block = self._entry_allowed(tier)
            if block:
                self.last_reason[b.code] = f"{reason}/{block}"
                self._log_trigger(b, lv, reason, block, hms)
                self._note(f"{b.code}/{block}", "{}{} {} 종합 {:.0f} {} → 미진입: {} (점수 순 뒤로 밀림)",
                           self.tag, b.code, lv["strategy"], sc, _TIER_KO[tier], block)
                continue
            await self._enter(b, lv, reason, sc, hms, tier)

    async def _enter(self, b: Bar, lv: dict, reason: str, sc: float, hms: str, tier: str = "priority") -> None:
        shares, err = self._size(b.close, lv)
        if err:
            self.last_reason[b.code] = err
            self._log_trigger(b, lv, reason, err, hms)
            return
        self._log_trigger(b, lv, reason, None, hms)
        logger.info("{}진입 시도 {} {} {} @{:,.0f} x{} 종합 {:.0f} {}", self.tag, b.code, lv["strategy"],
                    reason, b.close, shares, sc, _TIER_KO[tier])
        rule = self.c.exit_rule(lv["strategy"])
        sp, tp = entry_levels(b.close, rule)
        pos = state.arm(self.mode, b.code, lv["strategy"], self.wl_date, self.trade_date, sp, tp,
                        note=f"trigger={reason} bar={b.key} px={b.close:.0f} x{shares} exit={rule.label()} "
                             f"tier={tier} score={sc:.0f}")
        if not ledger.claim(self.mode, b.code, shares):
            # 다른 봇(스톡봇·대장주)이 이미 잡은 종목 — 더블 매수 방지, 이 종목은 오늘 포기
            state.drop(pos, state.DROP_NOFILL, "점유충돌")
            self.last_reason[b.code] = "점유충돌"
            logger.warning("{}진입 포기 {} — 다른 봇이 이미 점유 (오늘 제외)", self.tag, b.code)
            _notify(f"{self.tag}진입 포기 {_name(b.code)} — 다른 봇이 이미 점유 (오늘 제외)")
            self.watch.pop(b.code, None)
            if self.stream:
                self.stream.unsubscribe(b.code)
            self._promote_reserve()
            return
        self._pending_order.add(b.code)
        try:
            res = await asyncio.to_thread(orders.place, self.mode, b.code, "buy", shares, b.close,
                                          self.broker if self.mode != "dryrun" else None)
        except SystemExit as e:
            state.drop(pos, state.DROP_NOFILL, str(e))
            ledger.release(self.mode, b.code)
            self._pending_order.discard(b.code)
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("주문 예외 {}: {}", b.code, e)
            res = {"filled": False, "filled_qty": 0, "px": 0.0, "order_no": None, "error": str(e)}
        finally:
            self._pending_order.discard(b.code)
        if not res.get("filled"):
            self.nofill[b.code] = self.nofill.get(b.code, 0) + 1
            logger.warning("{}진입 미체결 {} ({}/{}): {}", self.tag, b.code, self.nofill[b.code], _NOFILL_MAX,
                           res.get("error"))
            _notify(f"⚠️ {self.tag}진입 미체결 {_name(b.code)} {shares}주 @ {b.close:,.0f} ({self.nofill[b.code]}/{_NOFILL_MAX}): "
                    f"{res.get('error')}{' → 오늘 감시 제외' if self.nofill[b.code] >= _NOFILL_MAX else ''}")
            state.drop(pos, state.DROP_NOFILL, str(res.get("error")))
            ledger.release(self.mode, b.code)                  # 미체결 점유 회수
            if self.nofill[b.code] >= _NOFILL_MAX:
                self.watch.pop(b.code, None)
                self.last_reason[b.code] = state.DROP_NOFILL
                if self.stream:
                    self.stream.unsubscribe(b.code)
                self._promote_reserve()
            return
        qty, px = int(res["filled_qty"]), float(res["px"])
        if res.get("cancel_failed"):
            _notify(f"🚨 {self.tag}잔량 취소 실패 {_name(b.code)} — 미체결 잔량이 뒤늦게 체결될 수 있습니다. HTS 확인 필요")
        if qty != shares:
            _notify(f"{'🚨' if qty > shares else '⚠️'} {self.tag}{'초과체결' if qty > shares else '부분체결'} {_name(b.code)} "
                    f"목표 {shares}주 → 실제 {qty}주 — 보유수량을 실제값으로 기록합니다")
        state.entered(pos, res.get("order_no"), qty, px)
        sp, tp = entry_levels(px, rule)
        state.holding(pos, qty, px, sp, tp)
        self.holdings[b.code] = pos
        self.watch.pop(b.code, None)
        self._promote_reserve()      # 보유로 옮긴 감시 자리를 다음 순위로 메운다
        self.new_today += 1
        if tier == "normal":
            self.new_normal_today += 1
        ledger.record(self.mode, pos, "buy", qty, px,
                      f"스윙 진입 {lv['strategy']} ({reason}, 손절 {sp:,.0f} 익절 {_tp_str(tp)})", res)
        logger.info("{}진입 체결 {} {} {}주 @{:,.0f} 손절 {:,.0f} 익절 {} 규칙 {} {} (신규 {}/{} 일반 {}/{})",
                    self.tag, b.code, lv["strategy"], qty, px, sp, _tp_str(tp), rule.label(), _TIER_KO[tier],
                    self.new_today, self.c.max_new_per_day, self.new_normal_today, self._normal_cap())
        slots, slot_krw = self._shared()
        invested = sum(int(p.get("shares") or 0) * float(p.get("entry_px") or 0) for p in self.holdings.values())
        _notify(f"🟢 **스윙봇 매수** {_name(b.code)} x{qty} @ {px:,.0f}\n"
                f"손절 {sp:,.0f} (-{rule.stop_pct * 100:g}%) · 익절 {_tp_str(tp)}"
                f"{f' (+{rule.tp_pct * 100:g}%)' if tp else ''}"
                f"{f' · {rule.ma_exit}일선 이탈 청산' if rule.ma_exit else ''} · {lv['strategy']} {reason} · 종합 {sc:.0f} {_TIER_KO[tier]}\n"
                f"투입 {qty * px:,.0f}원 (슬롯 {slot_krw:,.0f}) · 스윙 보유 {len(self.holdings)}/{slots}종목 총 {invested:,.0f}원 · "
                f"신규 {self.new_today}/{self.c.max_new_per_day} · {_hms()[:2]}:{_hms()[2:4]}:{_hms()[4:6]}")

    # ── 청산 ─────────────────────────────────────────────────────
    async def _exit(self, pos: dict, reason: str, px: float) -> None:
        code = pos["code"]
        if code in self._pending_order:
            return
        self._pending_order.add(code)
        try:
            qty = int(pos.get("shares") or 0)
            res = await asyncio.to_thread(orders.place, self.mode, code, "sell", qty, px,
                                          self.broker if self.mode != "dryrun" else None)
        except Exception as e:  # noqa: BLE001
            logger.error("청산 주문 예외 {}: {}", code, e)
            res = {"filled": False, "filled_qty": 0, "px": 0.0, "error": str(e)}
        finally:
            self._pending_order.discard(code)
        if not res.get("filled"):
            # 청산 실패는 다음 틱에서 다시 시도한다 — 포지션은 살아 있다
            logger.error("{}청산 미체결 {} {}: {}", self.tag, code, reason, res.get("error"))
            _notify(f"🚨 {self.tag}청산 미체결 {_name(code)} {reason} {qty}주 @ {px:,.0f}: {res.get('error')} — 다음 틱에 재시도")
            pos["note"] = (pos.get("note") or "") + f" | 청산실패({reason}):{res.get('error')}"
            store.upsert_position(pos)
            return
        fq = int(res["filled_qty"])
        if res.get("cancel_failed"):
            _notify(f"🚨 {self.tag}매도 잔량 취소 실패 {_name(code)} — 미체결 잔량이 뒤늦게 체결될 수 있습니다. HTS 확인 필요")
        if fq < qty and self.mode != "dryrun":
            # 부분 청산 — 남은 수량으로 포지션 유지, 다음 판정에서 마저 판다
            pos["shares"] = qty - fq
            pos["note"] = (pos.get("note") or "") + f" | 부분청산 {fq}/{qty}@{res['px']:.0f}({reason})"
            store.upsert_position(pos)
            logger.warning("부분 청산 {} {}/{} — 잔여 유지", code, fq, qty)
            _notify(f"⚠️ {self.tag}부분 청산 {_name(code)} {reason} {fq}/{qty}주 @ {res['px']:,.0f} — 잔여 {qty - fq}주 유지")
            ledger.record(self.mode, pos, "sell", fq, float(res["px"]), f"스윙 부분청산 {reason}", res)
            return
        state.exit_(pos, reason, float(res["px"]), self.trade_date, res.get("order_no"))
        self.holdings.pop(code, None)
        ledger.record(self.mode, pos, "sell", fq, float(res["px"]), f"스윙 청산 {reason}", res)
        ledger.release(self.mode, code)
        if self.stream and code not in self.watch:
            self.stream.unsubscribe(code)
        pnl = (float(res["px"]) / float(pos["entry_px"]) - 1) * 100 if pos.get("entry_px") else 0.0
        logger.info("{}청산 체결 {} {} {}주 @{:,.0f} (진입 {:,.0f} · {:+.2f}%) 잔여 보유 {}", self.tag, code,
                    reason, fq, float(res["px"]), float(pos.get("entry_px") or 0), pnl, len(self.holdings))
        _notify(f"{'🟢' if pnl >= 0 else '🔴'} {self.tag}청산 {_name(code)} {reason} {fq}주 @ {res['px']:,.0f}원 "
                f"(진입 {float(pos.get('entry_px') or 0):,.0f} · {pnl:+.2f}%) · 잔여 보유 {len(self.holdings)} · "
                f"시간 {_hms()[:2]}:{_hms()[2:4]}:{_hms()[4:6]}")

    # ── 15:20 ────────────────────────────────────────────────────
    def _held_days(self, pos: dict) -> int:
        """진입일 이후 지난 거래일 수 (진입 당일 = 0). 달력은 지수 일봉."""
        prev = (datetime.strptime(self.trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        after = store.trading_dates(pos["entry_date"], prev, IDX_CODE)
        return len([d for d in after if d > pos["entry_date"]]) + (1 if self.trade_date > pos["entry_date"] else 0)

    def _ma_today(self, code: str, close: float, n: int) -> float | None:
        """당일 종가를 포함한 n일 이동평균 (전일까지 n-1개 + 오늘 close)."""
        start = (datetime.strptime(self.trade_date, "%Y%m%d") - timedelta(days=n * 3)).strftime("%Y%m%d")
        prev = (datetime.strptime(self.trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        df = store.load_daily([code], start, prev).get(code)
        if df is None or len(df) < n - 1:
            return None
        closes = list(df["close"].tail(n - 1)) + [close]
        return float(sum(closes) / len(closes))

    async def eod(self) -> None:
        if self.eod_done:
            return
        self.eod_done = True
        logger.info("{}15:20 마감 판정 — 보유 {}", self.tag, len(self.holdings))
        for code, pos in list(self.holdings.items()):
            s = self.session.get(code)
            if not s:
                continue
            rule = self.c.exit_rule(pos.get("strategy"))
            r = exits.check_eod(pos, s["last"], rule, held_days=self._held_days(pos),
                                ma=self._ma_today(code, s["last"], rule.ma_exit) if rule.ma_exit else None)
            if r:
                await self._exit(pos, r[1], r[2])
        for r in self._reserve:          # 끝까지 자리가 안 난 예비 후보
            self._log_no_trigger(r["code"], state.DROP_NOSLOT, strategy=r["strategy"])
        # 미트리거 감시 종목 — 사유·당일 OHLC 기록
        for code, lv in self.watch.items():
            s = self.session.get(code)
            row = {"date": self.wl_date, "code": code, "strategy": lv["strategy"],
                   "trade_date": self.trade_date}
            if s:
                row.update(day_open=s["open"], day_high=s["session_high"],
                           day_low=s["session_low"], day_close=s["last"])
            reason = self.last_reason.get(code)
            if reason is None:
                reason = "틱없음" if not s else "미판정"
            existing = [x for x in store.load_signals(self.wl_date)
                        if x["code"] == code and x["strategy"] == lv["strategy"]]
            if not (existing and existing[0].get("trigger_time")):
                row["no_trigger_reason"] = reason
            store.log_signal(row)
        store.mark_run(self.trade_date, "live", "ok",
                       f"mode={self.mode} hold={len(self.holdings)} new={self.new_today} watch={len(self.watch)}")
        logger.info("{}마감 요약: {}", self.tag, self._monitor_summary())
        _notify(f"{self.tag}15:20 마감 — 보유 {len(self.holdings)} 신규 {self.new_today}/{self.c.max_new_per_day} "
                f"(일반 {self.new_normal_today}/{self._normal_cap()}) 감시 {len(self.watch)}\n{self._monitor_summary()}")

    # ── 실행 ─────────────────────────────────────────────────────
    def _mark_running(self) -> None:
        """runs(live) = running 하트비트 — 대시보드 '장중' 배지용 (EOD 에 ok 로 덮임)."""
        try:
            store.mark_run(self.trade_date, "live", "running",
                           f"mode={self.mode} hold={len(self.holdings)} new={self.new_today} "
                           f"watch={len(self.watch)} {_hms()[:4]}")
        except Exception as e:  # noqa: BLE001
            logger.debug("mark_run(running) 실패: {}", e)

    def _monitor_summary(self) -> str:
        """분봉 감시 현황 한 줄 — 로그탭에서 '살아있나·왜 안 사나' 를 보기 위한 것."""
        ticked = [cd for cd in self.watch if cd in self.session]
        bars = sum(self.session[cd]["bars"] for cd in ticked)
        cnt = Counter(v.split("/")[0] for cd, v in self.last_reason.items() if cd in self.watch)
        top = ", ".join(f"{k} {n}" for k, n in cnt.most_common(5))
        return (f"{self._ws_summary()} · 감시 {len(self.watch)} (틱수신 {len(ticked)} · 확정봉 {bars}) "
                f"보유 {len(self.holdings)} 신규 {self.new_today}/{self.c.max_new_per_day} "
                f"(일반 {self.new_normal_today}/{self._normal_cap()}) 대기 {len(self._armed)} 확인중 {len(self._confirm)} "
                f"진입가능={self._entry_allowed() or 'OK'} 미트리거 사유: {top or '-'}")

    def _ws_summary(self) -> str:
        """소켓 상태 한 조각 — 연결/끊김, 등록 건수, 재연결 횟수, 마지막 틱 경과."""
        st = self.stream
        if st is None:
            return "WS 미시작"
        age = f"{time.monotonic() - self._last_tick_at:.0f}초전" if self._last_tick_at is not None else "없음"
        return (f"WS {'연결' if st.connected else '끊김'} 등록 {len(st.registered)}/{len(st.codes)}"
                f"{f' 거부 {len(st.rejected)}' if st.rejected else ''}"
                f"{f' 재연결 {st.reconnects}회' if st.reconnects else ''} 틱 {self._ticks:,} (마지막 {age})")

    def _check_tick_silence(self) -> None:
        """장중인데 틱이 오래 안 오면 경고 — 연결은 살아 있는데 데이터가 없는 경우를 로그로 드러낸다."""
        if self.stream is None or not self.stream.connected or not ("090100" <= _hms() < EOD_TIME):
            return
        limit = max(120.0, 2.0 * self.c.bar_sec)
        ref = self._last_tick_at
        if ref is None:
            return                                   # 첫 틱 전 — 등록 직후는 조용할 수 있다
        silent = time.monotonic() - ref
        if silent < limit:
            self._silent_warned_at = None
            return
        if self._silent_warned_at is None or time.monotonic() - self._silent_warned_at >= 300:
            logger.warning("{}틱 무수신 {:.0f}초 — {} (연결은 살아있음: 거래 정지/한산? 서버 지연?)",
                           self.tag, silent, self._ws_summary())
            _notify(f"⚠️ {self.tag}틱 무수신 {silent:.0f}초 — {self._ws_summary()}")
            self._silent_warned_at = time.monotonic()

    async def _eod_timer(self) -> None:
        last_hb = last_log = time.time()
        while _hms() < EOD_TIME:
            await asyncio.sleep(5)
            if time.time() - last_hb >= 60:
                self._mark_running()
                last_hb = time.time()
                self._check_tick_silence()
            if time.time() - last_log >= max(60, self.c.bar_sec):
                logger.info("{}{} {}", self.tag, _hms()[:4], self._monitor_summary())
                last_log = time.time()
        await self.eod()

    def _trading_day_ok(self) -> bool:
        """휴장일이면 장중 루프를 돌리지 않는다.

        판정은 대장주·스톡봇과 같은 모듈(live.runner._is_trading_day) 을 쓴다 —
        KIS 국내휴장일 API 1순위, 실패하면 수동보강·exchange_calendars·주말 순.
        """
        from stock_bot.live import runner as _runner
        if self.mode != "dryrun" and _runner._holiday_broker is None:
            try:
                _runner._holiday_broker = self.broker      # KIS 달력 기준으로 판정
            except Exception as e:  # noqa: BLE001
                logger.warning("휴장일 판정용 브로커 준비 실패 — 달력 폴백: {}", e)
        try:
            return _runner._is_trading_day(datetime.strptime(self.trade_date, "%Y%m%d"))
        except Exception as e:  # noqa: BLE001
            logger.warning("휴장일 판정 실패 — 거래일로 간주하고 진행: {}", e)
            return True

    async def run(self) -> None:
        if not self._trading_day_ok():
            logger.info("{}{} 는 휴장일 — 장중 감시 실행 안 함", self.tag, self.trade_date)
            store.mark_run(self.trade_date, "live", "skip", "휴장일")
            return
        codes = self.prepare()
        if not codes:
            logger.warning("구독할 종목 없음 — 종료")
            store.mark_run(self.trade_date, "live", "skip", "no codes")
            return
        if _hms() >= STOP_TIME:
            logger.warning("장 종료 후 실행 — 종료")
            return
        self._mark_running()
        self.stream = SwingTickStream(
            codes, bar_sec=self.c.bar_sec, store_bar_sec=self.c.bar_store_sec,
            on_bar=self.on_bar, on_store_bar=self.on_store_bar, on_tick=self.on_tick,
            on_gap=self.on_gap, stop_at=STOP_TIME,
        )
        timer = asyncio.create_task(self._eod_timer())
        logger.info("{}WS 시작 — 구독 {}종목 (보유 {} 감시 {}) 봉 {}초 저장봉 {}초", self.tag, len(codes),
                    len(self.holdings), len(self.watch), self.c.bar_sec, self.c.bar_store_sec)
        _notify(f"{self.tag}장중 감시 시작 — 구독 {len(codes)}종목 (보유 {len(self.holdings)} 감시 {len(self.watch)})\n"
                f"진입 창 {self.c.entry_from[:2]}:{self.c.entry_from[2:4]}~{self.c.entry_until[:2]}:{self.c.entry_until[2:4]} · "
                f"종합점수 하한 {self.c.entry_min_score:.0f} · 하루 상한 {self.c.max_new_per_day} "
                f"(일반 등급 {self._normal_cap()}) · 우선 등급 {len(self._priority)}종목(★ 즉시) · "
                f"일반 등급 다음 {self.c.normal_confirm_bars}봉 유지 확인 · "
                f"레짐 {'OK' if self.regime_ok else '차단'} (mult {self.size_mult})")
        try:
            await self.stream.run()
        finally:
            timer.cancel()
            logger.info("{}WS 종료 {} — {}", self.tag, _hms()[:4], self._ws_summary())
            if _hms() < EOD_TIME:
                _notify(f"⚠️ {self.tag}WS 조기 종료 {_hms()[:2]}:{_hms()[2:4]} — {self._ws_summary()}\n"
                        f"보유 {len(self.holdings)} 종목의 손절/익절 감시가 멈췄습니다. 로그 확인 필요")
            if self._armed_task and not self._armed_task.done():
                self._armed_task.cancel()
            if self._backfill_task and not self._backfill_task.done():
                self._backfill_task.cancel()
            if self.stream.rejected:
                for cd, why in self.stream.rejected:
                    if cd in self.watch:
                        self._log_no_trigger(cd, f"구독거부 {why}")
            await self.eod()
            if self._broker:
                self._broker.close()


def main(trade_date: str | None = None) -> None:
    asyncio.run(SwingLive(trade_date=trade_date).run())
