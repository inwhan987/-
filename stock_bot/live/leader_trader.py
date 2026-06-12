"""대장주 눌림목 전략 실전 모듈 (backtest_leader_pullback.py 의 라이브 구현).

흐름 (백테스트 06-09~06-12 확정 설정과 동일):
  · 대상   : 당일 data/leader_picks/날짜.json 의 1등 섹터 top3 바스켓
             — 70% 룰: 2·3등 등락률 ≥ 1등 × leader_top3_ratio 일 때만 편입
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
from stock_bot.market_calendar import KST as _KST
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


class LeaderTrader:
    """하루 단위 상태머신: watching → holding → done."""

    def __init__(self, broker: KISBroker) -> None:
        self.broker = broker
        self._date = ""            # 상태가 로드된 날짜 (YYYY-MM-DD)
        self._state: dict[str, Any] = {}
        self._basket: list[dict[str, Any]] = []
        self._trade_start: tuple[int, int] = (9, 30)
        self._no_picks_logged = ""  # picks 미존재 로그 1회 제한용 날짜
        self._soft_logged = ""      # 상한가컷 로그 중복 방지 (code:bar_time)

    # ── 상태 영속 ────────────────────────────────────────────────────
    def _state_path(self, date: str) -> Path:
        return _STATE_DIR / f"{date}.json"

    def _load_day(self, date: str) -> None:
        """날짜가 바뀌면 picks·상태 재로드."""
        self._date = date
        self._basket = []
        path = self._state_path(date)
        try:
            self._state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._state = {"status": "watching"}

        picks_path = _PICKS_DIR / f"{date}.json"
        if not picks_path.exists():
            return
        try:
            picks = json.loads(picks_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("leader_trader: picks 파싱 실패 {} — {}", picks_path.name, e)
            return

        sel = str(picks.get("selected_at", "09:30"))
        self._trade_start = _parse_hm(sel, (9, 30))

        leaders = picks.get("leaders") or []
        if not leaders:
            return
        top3 = leaders[0].get("top3") or []
        if not top3:  # top3 기록 이전 포맷 → 1등만
            top3 = [{"rank": 1, "code": leaders[0]["code"],
                     "name": leaders[0].get("name", ""),
                     "change_pct": leaders[0].get("change_pct", 0)}]
        top3 = sorted(top3, key=lambda x: x.get("rank", 9))

        # 70% 룰: 2·3등은 1등 등락률 × ratio 이상일 때만 바스켓 편입
        lead_chg = float(top3[0].get("change_pct", 0))
        thresh = lead_chg * settings.leader_top3_ratio
        basket = [top3[0]] + [
            m for m in top3[1:] if float(m.get("change_pct", 0)) >= thresh
        ]
        # 기존 전략 종목과 겹치면 제외
        own = {_bare(s) for s in settings.symbols}
        basket = [m for m in basket if _bare(m["code"]) not in own]
        self._basket = basket
        if basket and self._state.get("status") == "watching":
            logger.info(
                "leader_trader: {} 바스켓 {} (선별 {:02d}:{:02d}, 70%기준 {:+.1f}%)",
                date,
                ", ".join(f"{m.get('name', '')}({m['code']})" for m in basket),
                *self._trade_start, thresh,
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

    # ── 메인 틱 ──────────────────────────────────────────────────────
    def tick(self) -> None:
        if not settings.leader_trade_enabled:
            return
        now = datetime.now(tz=_KST)
        date = f"{now:%Y-%m-%d}"
        if date != self._date:
            self._load_day(date)

        status = self._state.get("status", "watching")
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
        close_t = _parse_hm(settings.leader_close_time, (14, 55))
        if (now.hour, now.minute) >= close_t:
            return  # 마감 직전엔 신규 진입 안 함
        self._scan_entries(now)

    # ── 진입 탐색 ────────────────────────────────────────────────────
    def _scan_entries(self, now: datetime) -> None:
        iv = settings.leader_interval_min
        w = settings.leader_w
        pull = settings.leader_max_pull_pct / 100
        skipped = self._state.setdefault("skipped", {})  # code → 사유 (재평가 안 함)

        for m in self._basket:  # rank 순 → 동시 신호 시 순위 우선
            code = _bare(m["code"])
            if code in skipped:
                continue
            try:
                sig = self._check_signal(code, now, iv, w, pull)
            except Exception as e:
                logger.warning("leader_trader: {} 신호 평가 실패 — {}", code, e)
                continue
            if sig is None:
                continue
            if sig.get("skip"):  # 그날 영구 보류 (붕괴컷 등)
                skipped[code] = sig["skip"]
                self._save_state()
                logger.info("leader_trader: {} 보류 — {}", code, sig["skip"])
                continue
            if sig.get("soft_skip"):  # 이번 신호만 스킵 (상한가컷 — 다음 스윙저점은 가능)
                key = f"{code}:{sig['bar_time']}"
                if self._soft_logged != key:
                    logger.info("leader_trader: {} 신호 스킵 — {}", code, sig["soft_skip"])
                    self._soft_logged = key
                continue
            if self._enter(m, code, sig, now):
                return  # 하루 1종목 — 진입 성공 시 종료

    def _check_signal(
        self, code: str, now: datetime, iv: int, w: int, pull: float
    ) -> dict[str, Any] | None:
        """마지막 확정봉이 스윙저점 확정봉이면 신호 dict, 아니면 None.

        backtest simulate() 와 동일 판정: pre_high/floor → W 스윙저점 →
        NODMG 붕괴컷 → 상한가컷. 반환 {"skip": 사유} 는 그날 영구 보류.
        """
        bars = self.broker.get_minute_ohlcv_today(code, interval_min=iv)
        if not bars:
            return None
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
        start_hms = f"{self._trade_start[0]:02d}{self._trade_start[1]:02d}00"

        # Phase 1: 9:00~선별시각 전고점
        ph_idx = [j for j in range(n) if times[j] < start_hms]
        if not ph_idx:
            return None
        ph_j = max(ph_idx, key=lambda j: highs[j])
        pre_high = highs[ph_j]
        floor = pre_high * (1 - pull)

        # Phase 2: 마지막 확정봉 j 가 스윙저점(i = j-w) 확정봉인지
        j = n - 1
        if times[j] < start_hms:
            return None
        i = j - w
        if not (
            i >= w
            and all(lows[i] <= lows[i - k] for k in range(1, w + 1))
            and all(lows[i] <= lows[i + k] for k in range(1, w + 1))
            and lows[i] >= floor
        ):
            return None
        # 붕괴컷: 전고점 이후 진입 전 floor 를 깼으면 그날 보류
        if any(lows[k] < floor for k in range(ph_j + 1, j)):
            return {"skip": f"붕괴컷 (floor {floor:,.0f} 이탈)"}

        ref = lows[i]
        entry_est = closes[j]  # 확정봉 종가 (실체결은 시장가)
        stop = ref * (1 - settings.leader_stop_buf_pct / 100)
        tp_px = entry_est * (1 + settings.leader_tp_pct / 100)

        # 상한가컷: 전일종가 = 현재가 / (1 + 등락률)
        quote = self.broker.get_quote(code)
        prev_close = (
            quote.price / (1 + quote.change_pct / 100) if quote.change_pct > -100 else 0
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

    def _enter(
        self, member: dict[str, Any], code: str, sig: dict[str, Any], now: datetime
    ) -> bool:
        price = sig["price_now"] or sig["entry_est"]
        qty = int(settings.leader_budget_krw // price)
        if qty < 1:
            logger.warning(
                "leader_trader: {} 예산 부족 (예산 {:,.0f} < 현재가 {:,.0f})",
                code, settings.leader_budget_krw, price,
            )
            self._state.setdefault("skipped", {})[code] = "예산 부족"
            self._save_state()
            return False
        try:
            resp = self.broker.place_order(code, "buy", qty, order_type="market")
        except OrderRejectedError as e:
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
            code, qty, entry, sig["stop"], tp_px,
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
            logger.warning("leader_trader: {} 현재가 조회 실패 — {}", code, e)
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
        try:
            resp = self.broker.place_order(code, "sell", qty, order_type="market")
        except OrderRejectedError as e:
            notify(f"🚫 **대장주봇 매도 거부** {st.get('name', '')}({code}) x{qty}: {e}")
            logger.error("leader_trader: 매도 거부 {} — {}", code, e)
            return  # 다음 틱 재시도

        entry = float(st["entry"])
        net = (price * (1 - _SELL_COMM) / (entry * (1 + _BUY_COMM)) - 1) * 100
        st.update({
            "status": "done", "exit": price,
            "exit_at": f"{now:%H:%M:%S}", "exit_reason": reason, "net_pct": round(net, 2),
        })
        self._save_state()
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
            "leader_trader: 청산 {} [{}] @ {:,.0f} net {:+.2f}%", code, reason, price, net,
        )
