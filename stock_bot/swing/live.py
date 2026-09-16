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


def _hms() -> str:
    return time.strftime("%H%M%S")


def _notify(msg: str) -> None:
    try:
        from stock_bot.notify.discord import notify
        notify(msg)
    except Exception as e:  # noqa: BLE001
        logger.debug("notify 실패: {}", e)


class SwingLive:
    def __init__(self, c: SwingCfg | None = None, trade_date: str | None = None):
        self.c = c or cfg()
        self.mode = self.c.mode
        self.trade_date = trade_date or datetime.now().strftime("%Y%m%d")
        self.wl_date: str | None = None
        self.watch: dict[str, dict] = {}          # code → watchlist 행 (신규 감시)
        self.holdings: dict[str, dict] = {}       # code → positions 행 (HOLDING)
        self.session: dict[str, dict] = {}        # code → {open, session_high, session_low, last, bars}
        self.last_reason: dict[str, str] = {}     # code → 마지막 미트리거 사유
        self.nofill: dict[str, int] = {}
        self.regime_ok, self.size_mult = True, 1.0
        self.new_today = 0
        self.eod_done = False
        self.stream: SwingTickStream | None = None
        self._broker: KISBroker | None = None
        self._pending_order: set[str] = set()
        self._armed: dict[str, tuple] = {}        # code → (bar, lv, reason, score, hms) 모음 창 대기
        self._armed_task: asyncio.Task | None = None
        self._noted: set[str] = set()             # 같은 보류/차단 로그 하루 1회

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
        logger.info("[{}] 진입 규칙: 창 {}~{} 종합점수 하한 {:.0f} 모음 창 {}초 하루 상한 {}",
                    self.mode, self.c.entry_from, self.c.entry_until, self.c.entry_min_score,
                    self.c.entry_batch_sec, self.c.max_new_per_day)
        logger.info("[{}] {} 준비: 보유 {} 신규감시 {} (wl={}) 레짐={} mult={}",
                    self.mode, self.trade_date, len(self.holdings), len(self.watch),
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
                    sp, tp = entry_levels(float(p["entry_px"]), self.c)
                    state.holding(p, q, float(p["entry_px"]), sp, tp)
                    self.holdings[p["code"]] = p
                else:
                    state.drop(p, state.DROP_NOFILL, "재시작 시 잔고 없음")
        self.new_today = sum(1 for p in store.load_positions(self.mode)
                             if p.get("entry_date") == self.trade_date
                             and p.get("state") in (state.ENTERED, state.HOLDING, state.EXIT))

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
        n_new = self.c.watch_new - max(0, len(self.holdings) - self.c.watch_hold)
        picked = 0
        for r in rows:
            cd = r["code"]
            if cd in self.holdings or cd in self.watch:
                continue
            if picked >= n_new:
                self._log_no_trigger(cd, state.DROP_NOSLOT, strategy=r["strategy"])
                continue
            self.watch[cd] = r
            picked += 1
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
        self._sess(t)
        pos = self.holdings.get(t.code)
        if pos and t.code not in self._pending_order:
            r = exits.check_tick(pos, t.price, self.c)
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
            r = exits.check_bar(pos, b, self.c)
            store.upsert_position(pos)                 # peak/trail_on 갱신
            if r:
                await self._exit(pos, r[1], r[2])
            return
        lv = self.watch.get(b.code)
        if lv is None:
            return
        await self._maybe_enter(b, lv, s)

    async def on_gap(self, gap_sec: float, codes: list[str]) -> None:
        """끊긴 구간은 복구 불가 → REST 1분봉으로 세션 고저·peak 백필 (명세 11절)."""
        logger.warning("WS 공백 {:.0f}초 — REST 백필 {}종목", gap_sec, len(codes))
        targets = [cd for cd in codes if cd in self.holdings] + \
                  [cd for cd in codes if cd not in self.holdings]
        for cd in targets:
            try:
                rows = await asyncio.to_thread(self.broker.get_minute_ohlcv, cd, 1, 30)
            except Exception as e:  # noqa: BLE001
                logger.debug("백필 실패 {}: {}", cd, e)
                continue
            if not rows:
                continue
            hi = max(r["high"] for r in rows)
            lo = min(r["low"] for r in rows)
            s = self.session.setdefault(cd, {"open": rows[-1]["open"], "session_high": hi,
                                             "session_low": lo, "last": rows[0]["close"], "bars": 0})
            s["session_high"] = max(s["session_high"], hi)
            s["session_low"] = min(s["session_low"], lo)
            pos = self.holdings.get(cd)
            if pos:
                pos["peak"] = max(float(pos.get("peak") or pos["entry_px"]), hi)
                store.upsert_position(pos)

    # ── 진입 ─────────────────────────────────────────────────────
    def _entry_allowed(self) -> str | None:
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
        shares = int(self._shared()[1] * self.size_mult // px)
        vma = lv.get("ref_value_ma20")
        if vma and self.c.max_order_share > 0:
            cap = int(self.c.max_order_share * float(vma) // px)
            if cap < shares:
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
        if not ok:
            self.last_reason[b.code] = reason
            return
        if b.code in self._armed:
            return                                   # 이미 모음 창에서 대기 중
        block = self._entry_allowed()
        if block:
            self.last_reason[b.code] = f"{reason}/{block}"
            self._log_trigger(b, lv, reason, block)
            self._note(f"{b.code}/{block}", "[{}] 트리거 감지 {} {} {} @{:,.0f} → 미진입: {}",
                       self.mode, b.code, lv["strategy"], reason, b.close, block)
            return
        sc = self._score(lv)
        if sc < self.c.entry_min_score:
            self.last_reason[b.code] = f"{reason}/점수보류"
            self._log_trigger(b, lv, reason, "점수보류")
            self._note(f"{b.code}/점수보류", "[{}] 트리거 감지 {} {} {} @{:,.0f} 종합 {:.0f} < 하한 {:.0f} → 점수보류",
                       self.mode, b.code, lv["strategy"], reason, b.close, sc, self.c.entry_min_score)
            return
        self._armed[b.code] = (b, lv, reason, sc, _hms())
        logger.info("[{}] 트리거 {} {} {} @{:,.0f} 종합 {:.0f} — {}초 모음 창 대기 ({}건)",
                    self.mode, b.code, lv["strategy"], reason, b.close, sc, self.c.entry_batch_sec, len(self._armed))
        if self._armed_task is None or self._armed_task.done():
            self._armed_task = asyncio.create_task(self._flush_armed())

    async def _flush_armed(self) -> None:
        """모음 창이 닫히면 종합점수 높은 순으로 진입. 하루 상한·슬롯은 한 건씩 다시 확인."""
        await asyncio.sleep(max(0, self.c.entry_batch_sec))
        items = sorted(self._armed.values(), key=lambda x: -x[3])
        self._armed.clear()
        if not items:
            return
        logger.info("[{}] 진입 순서(종합점수순): {}", self.mode,
                    " > ".join(f"{b.code} {sc:.0f}" for b, _, _, sc, _ in items))
        for b, lv, reason, sc, hms in items:
            if b.code not in self.watch or b.code in self.holdings:
                continue                             # 그 사이 감시 제외/보유
            block = self._entry_allowed()
            if block:
                self.last_reason[b.code] = f"{reason}/{block}"
                self._log_trigger(b, lv, reason, block, hms)
                self._note(f"{b.code}/{block}", "[{}] {} {} 종합 {:.0f} → 미진입: {} (점수 순 뒤로 밀림)",
                           self.mode, b.code, lv["strategy"], sc, block)
                continue
            await self._enter(b, lv, reason, sc, hms)

    async def _enter(self, b: Bar, lv: dict, reason: str, sc: float, hms: str) -> None:
        shares, err = self._size(b.close, lv)
        if err:
            self.last_reason[b.code] = err
            self._log_trigger(b, lv, reason, err, hms)
            return
        self._log_trigger(b, lv, reason, None, hms)
        logger.info("[{}] 진입 시도 {} {} {} @{:,.0f} x{} 종합 {:.0f}", self.mode, b.code, lv["strategy"],
                    reason, b.close, shares, sc)
        sp, tp = entry_levels(b.close, self.c)
        pos = state.arm(self.mode, b.code, lv["strategy"], self.wl_date, self.trade_date, sp, tp,
                        note=f"trigger={reason} bar={b.key} px={b.close:.0f} x{shares}")
        if not ledger.claim(self.mode, b.code, shares):
            # 다른 봇(스톡봇·대장주)이 이미 잡은 종목 — 더블 매수 방지, 이 종목은 오늘 포기
            state.drop(pos, state.DROP_NOFILL, "점유충돌")
            self.last_reason[b.code] = "점유충돌"
            self.watch.pop(b.code, None)
            if self.stream:
                self.stream.unsubscribe(b.code)
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
            state.drop(pos, state.DROP_NOFILL, str(res.get("error")))
            ledger.release(self.mode, b.code)                  # 미체결 점유 회수
            if self.nofill[b.code] >= _NOFILL_MAX:
                self.watch.pop(b.code, None)
                self.last_reason[b.code] = state.DROP_NOFILL
                if self.stream:
                    self.stream.unsubscribe(b.code)
            return
        qty, px = int(res["filled_qty"]), float(res["px"])
        state.entered(pos, res.get("order_no"), qty, px)
        sp, tp = entry_levels(px, self.c)
        state.holding(pos, qty, px, sp, tp)
        self.holdings[b.code] = pos
        self.watch.pop(b.code, None)
        self.new_today += 1
        ledger.record(self.mode, pos, "buy", qty, px,
                      f"스윙 진입 {lv['strategy']} ({reason}, 손절 {sp:,.0f} 익절 {tp:,.0f})", res)
        _notify(f"[스윙:{self.mode}] 진입 {b.code} {lv['strategy']} {qty}주 @ {px:,.0f} ({reason})")

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
            logger.error("[{}] 청산 미체결 {} {}: {}", self.mode, code, reason, res.get("error"))
            pos["note"] = (pos.get("note") or "") + f" | 청산실패({reason}):{res.get('error')}"
            store.upsert_position(pos)
            return
        fq = int(res["filled_qty"])
        if fq < qty and self.mode != "dryrun":
            # 부분 청산 — 남은 수량으로 포지션 유지, 다음 판정에서 마저 판다
            pos["shares"] = qty - fq
            pos["note"] = (pos.get("note") or "") + f" | 부분청산 {fq}/{qty}@{res['px']:.0f}({reason})"
            store.upsert_position(pos)
            logger.warning("부분 청산 {} {}/{} — 잔여 유지", code, fq, qty)
            ledger.record(self.mode, pos, "sell", fq, float(res["px"]), f"스윙 부분청산 {reason}", res)
            return
        state.exit_(pos, reason, float(res["px"]), self.trade_date, res.get("order_no"))
        self.holdings.pop(code, None)
        ledger.record(self.mode, pos, "sell", fq, float(res["px"]), f"스윙 청산 {reason}", res)
        ledger.release(self.mode, code)
        if self.stream and code not in self.watch:
            self.stream.unsubscribe(code)
        pnl = (float(res["px"]) / float(pos["entry_px"]) - 1) * 100 if pos.get("entry_px") else 0.0
        _notify(f"[스윙:{self.mode}] 청산 {code} {reason} @ {res['px']:,.0f} ({pnl:+.2f}%)")

    # ── 15:20 ────────────────────────────────────────────────────
    def _held_days(self, pos: dict) -> int:
        """진입일 이후 지난 거래일 수 (진입 당일 = 0). 달력은 지수 일봉."""
        prev = (datetime.strptime(self.trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        after = store.trading_dates(pos["entry_date"], prev, IDX_CODE)
        return len([d for d in after if d > pos["entry_date"]]) + (1 if self.trade_date > pos["entry_date"] else 0)

    def _ma20_today(self, code: str, close: float) -> float | None:
        start = (datetime.strptime(self.trade_date, "%Y%m%d") - timedelta(days=60)).strftime("%Y%m%d")
        prev = (datetime.strptime(self.trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        df = store.load_daily([code], start, prev).get(code)
        if df is None or len(df) < 19:
            return None
        closes = list(df["close"].tail(19)) + [close]
        return float(sum(closes) / len(closes))

    async def eod(self) -> None:
        if self.eod_done:
            return
        self.eod_done = True
        logger.info("[{}] 15:20 마감 판정 — 보유 {}", self.mode, len(self.holdings))
        for code, pos in list(self.holdings.items()):
            s = self.session.get(code)
            if not s:
                continue
            r = exits.check_eod(pos, s["last"], self.c, held_days=self._held_days(pos),
                                ma20=self._ma20_today(code, s["last"]) if self.c.exit_trend_break else None)
            if r:
                await self._exit(pos, r[1], r[2])
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
        return (f"감시 {len(self.watch)} (틱수신 {len(ticked)} · 확정봉 {bars}) 보유 {len(self.holdings)} "
                f"신규 {self.new_today}/{self.c.max_new_per_day} 대기 {len(self._armed)} "
                f"진입가능={self._entry_allowed() or 'OK'} 미트리거 사유: {top or '-'}")

    async def _eod_timer(self) -> None:
        last_hb = last_log = time.time()
        while _hms() < EOD_TIME:
            await asyncio.sleep(5)
            if time.time() - last_hb >= 60:
                self._mark_running()
                last_hb = time.time()
            if time.time() - last_log >= max(60, self.c.bar_sec):
                logger.info("[{}] {} {}", self.mode, _hms()[:4], self._monitor_summary())
                last_log = time.time()
        await self.eod()

    async def run(self) -> None:
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
        try:
            await self.stream.run()
        finally:
            timer.cancel()
            if self._armed_task and not self._armed_task.done():
                self._armed_task.cancel()
            if self.stream.rejected:
                for cd, why in self.stream.rejected:
                    if cd in self.watch:
                        self._log_no_trigger(cd, f"구독거부 {why}")
            await self.eod()
            if self._broker:
                self._broker.close()


def main(trade_date: str | None = None) -> None:
    asyncio.run(SwingLive(trade_date=trade_date).run())
