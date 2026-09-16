# -*- coding: utf-8 -*-
"""주문 — 세 모드가 여기서만 갈린다 (명세 1절).

  dryrun : 주문 없음. 체결가는 호출측이 준 px(봉 종가/틱가) 로 가상 체결
  paper  : KIS 모의계좌 시장가 주문 → 체결 조회로 확정
  live   : KIS 실전계좌. settings.kis_env 가 'real' 이어야만 열린다

안전장치:
  * mode 와 settings.kis_env 가 어긋나면(paper 모드인데 실전 키, 또는 그 반대)
    주문을 내지 않고 SystemExit. 실계좌 토글은 사용자만 바꾼다.
  * TRADE_DRY_RUN=true 면 KISBroker 가 어차피 안 보낸다 → dryrun 취급.
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

from stock_bot.broker.kis import KISBroker, OrderRejectedError
from stock_bot.config import settings

# 체결 확정 구조는 대장주봇(leader_trader._enter) 과 같다 — 2026-09-16 인바디 173주 중 100주만
# 5초 안에 붙고 잔량 취소로 슬롯의 57% 만 들어간 건에서 이식.
#   1) 시장가 → 체결조회. 잔량(rmn_qty) 이 0 인데 부족하면 부족분을 **재주문** (최대 _MARKET_RETRY 회)
#   2) 잔량이 살아있거나(rmn>0) 판정 불가(None) 면 재주문하지 않고 **계좌 잔고** 가 목표를 채우거나
#      예산(SWING_FILL_BLOCK_SEC) 이 다할 때까지 지켜본다 — 재주문하면 원주문이 마저 체결돼 이중 매수
#   3) 예산이 다하면 잔량 취소 → 잔고 변화분으로 체결수량 확정 (유령 잔량 차단)
#   4) 마지막에 한 번 더 잔고와 대조해 체결조회 스냅샷이 놓친 늦은 체결을 흡수
# 스윙은 asyncio.to_thread 안에서 돌아 대기가 틱 처리를 막지 않는다 → 대장주(5초 락예산)보다 길게 잡아도 된다.
_MARKET_RETRY = 3            # 부족분 재주문 최대 회차 (원주문 포함)
_RETRY_WAIT = 1.0            # 재주문 간격 — KIS 모의 유량 1건/초
_SETTLE_WAIT = 1.0           # 잔고 안정화 폴링 간격
_CANCEL_SETTLE = 0.7         # 취소 접수 후 잔고 반영 대기
_DEFAULT_BLOCK_SEC = 20.0    # settings.swing_fill_block_sec 없을 때


def _budget_sec() -> float:
    try:
        return max(3.0, float(getattr(settings, "swing_fill_block_sec", _DEFAULT_BLOCK_SEC)))
    except (TypeError, ValueError):
        return _DEFAULT_BLOCK_SEC


def _held(b: KISBroker, code: str) -> int | None:
    """브로커 실제 보유수량. 조회 실패면 None(=판단 보류)."""
    try:
        rows = b.get_positions()
    except Exception as e:  # noqa: BLE001
        logger.warning("잔고 조회 실패 {} — {}", code, e)
        return None
    for r in rows:
        if str(r.get("pdno", "")).strip() == code:
            return int(float(r.get("hldg_qty") or 0))
    return 0


def _moved(side: str, before: int | None, now: int | None) -> int | None:
    """주문 전후 잔고 변화 = 실제 체결수량. 매수는 증가분, 매도는 감소분."""
    if before is None or now is None:
        return None
    return max(0, now - before) if side == "buy" else max(0, before - now)


def _check_env(mode: str) -> None:
    if mode == "paper" and not settings.is_paper:
        raise SystemExit("스윙 모드 paper 인데 KIS_ENV 가 real — 주문 중단")
    if mode == "live" and settings.is_paper:
        raise SystemExit("스윙 모드 live 인데 KIS_ENV 가 paper — 주문 중단")


def place(mode: str, code: str, side: str, qty: int, px: float,
          broker: KISBroker | None = None) -> dict[str, Any]:
    """반환: {"filled": bool, "filled_qty": int, "px": 평균체결가, "order_no": str|None,
              "simulated": bool, "resp": 원응답|None, "error": str|None}

    filled 는 '전량 체결' 이 아니라 '1주 이상 체결' 이다. 부족분은 예산 안에서 재주문/대기로 채우고,
    그래도 남으면 잔량을 취소한 뒤 filled_qty 로 실제 수량을 준다(호출측이 shares 를 이 값으로 바꾼다).
    부분·초과 판정용으로 "target"(목표 수량), 잔량 취소 실패면 "cancel_failed"=True 를 함께 준다.
    """
    if qty <= 0:
        return {"filled": False, "filled_qty": 0, "px": 0.0, "order_no": None,
                "simulated": True, "resp": None, "error": "수량 0"}
    if mode == "dryrun" or settings.trade_dry_run:
        logger.info("[DRYRUN] {} {} x{} @ {:,.0f}", side, code, qty, px)
        return {"filled": True, "filled_qty": qty, "px": float(px), "order_no": None,
                "simulated": True, "resp": None, "error": None}

    _check_env(mode)
    own = broker is None
    b = broker or KISBroker()
    try:
        return _place_market(mode, b, code, side, qty, float(px))
    finally:
        if own:
            b.close()


def _place_market(mode: str, b: KISBroker, code: str, side: str, qty: int, px: float) -> dict[str, Any]:
    """시장가 주문 + 체결 확정 (파일 머리의 1)~4) 절차). qty 는 목표 수량."""
    budget = _budget_sec()
    held_before = _held(b, code)          # 잔고 대조 기준선
    deadline = time.monotonic() + budget
    filled_total = 0
    cost_total = 0.0
    resp: dict[str, Any] = {}
    odno = ""
    done = False                          # 잔고가 목표를 채운 것을 확인
    cancelled = None                      # None=취소 시도 없음 / True / False
    for attempt in range(1, _MARKET_RETRY + 1):
        want = qty - filled_total
        if want < 1:
            break
        if attempt > 1:
            if time.monotonic() < deadline:
                time.sleep(_RETRY_WAIT)
            # 재주문 직전 잔고 재확인 — 체결조회(rmn_qty) 는 모의투자에서 비어올 수 있다. 잔고가 진실.
            moved = _moved(side, held_before, _held(b, code))
            if moved is not None and moved > filled_total:
                logger.warning("[{}] {} {} 재주문 보류 — 체결조회 {}주였으나 잔고 변화 {}주", mode, side, code,
                               filled_total, moved)
                cost_total = cost_total * moved / filled_total if filled_total > 0 else moved * px
                filled_total = moved
                want = qty - filled_total
                if want < 1:
                    break
        try:
            resp = b.place_order(code, side, want, 0.0, order_type="market")
        except OrderRejectedError as e:
            if filled_total <= 0:
                logger.warning("[{}] 주문 거부 {} {} x{}: {}", mode, side, code, want, e)
                return {"filled": False, "filled_qty": 0, "px": 0.0, "order_no": None,
                        "simulated": False, "resp": None, "error": f"거부: {e}", "target": qty}
            logger.warning("[{}] {} {} 부족분 재주문 거부 x{} — {}", mode, side, code, want, e)
            break
        odno = str((resp.get("output") or {}).get("ODNO", "") or "")
        left = max(0.0, deadline - time.monotonic())
        try:
            fill = b.get_order_fill(code, resp, attempts=max(1, min(int(budget) + 1, int(left) + 1)), wait=1.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("[{}] 체결 조회 예외 {} {} — {}", mode, side, code, e)
            fill = None
        if fill is None:
            if held_before is None:
                # 체결조회도 잔고도 못 읽는다 — 확정할 근거가 없다. 잔량 취소로 마감시키고 '불명' 반환
                _cancel(mode, b, code, resp)
                logger.error("[{}] 체결 조회 실패 {} {} — 잔량 취소, 판단 보류", mode, side, code)
                return {"filled": False, "filled_qty": 0, "px": 0.0, "order_no": odno,
                        "simulated": False, "resp": resp, "error": "체결조회실패", "target": qty}
            # 조회 실패는 '체결 0' 이 아니다 — 잔량 판정 불가(None) 로 두고 아래에서 잔고로 확정한다
            f, avg, rmn = 0, px, None
        else:
            f = int(fill.get("filled_qty") or 0)
            avg = float(fill.get("avg_price") or 0.0) or px
            rmn = fill.get("rmn_qty")
        filled_total += f
        cost_total += f * avg
        if f >= want:
            break
        if rmn is None or rmn > 0:
            # 잔량이 죽었다는 증거가 없다 — 재주문 금지. 잔고가 목표를 채우거나 예산이 다할 때까지 지켜본다
            settled = None
            while time.monotonic() < deadline:
                time.sleep(_SETTLE_WAIT)
                q = _held(b, code)
                if q is None:
                    break
                settled = q
                mv = _moved(side, held_before, q)
                if mv is not None and mv >= qty:
                    done = True
                    break
            if not done:
                cancelled = _cancel(mode, b, code, resp)
                time.sleep(_CANCEL_SETTLE)
                q = _held(b, code)
                if q is not None:
                    settled = q
            mv = _moved(side, held_before, settled)
            if mv:
                cost_total = cost_total * mv / filled_total if filled_total > 0 else mv * px
                filled_total = mv
            logger.warning("[{}] {} {} {} — {}주 주문 중 {}주 체결 (잔량 {} · 예산 {:.0f}초)", mode, side, code,
                           "전량체결" if done else f"미체결 {max(0, want - filled_total)}주 취소", want, filled_total,
                           rmn if rmn is not None else "?", budget)
            break
        logger.warning("[{}] {} {} 시장가 부분체결 {}/{}주 (평균 {:,.0f}) — {}/{}회차, 부족분 {}주 재주문", mode, side,
                       code, f, want, avg, attempt, _MARKET_RETRY, want - f)

    # 유령 잔량 방지: 체결조회는 스냅샷이라 늦게 붙은 체결을 놓친다. 잔고 변화분이 진짜 체결량.
    actual = _moved(side, held_before, _held(b, code))
    if actual is not None and actual != filled_total:
        logger.warning("[{}] {} {} 체결수량 보정 {}주 → 잔고 변화 {}주", mode, side, code, filled_total, actual)
        cost_total = cost_total * actual / filled_total if filled_total > 0 and actual > 0 else actual * px
        filled_total = actual
    avg = cost_total / filled_total if filled_total > 0 else px
    logger.info("[{}] {} {} 주문 {} 체결 {}/{} @ {:,.0f}{}", mode, side, code, odno, filled_total, qty, avg,
                " (잔량 취소 실패 — HTS 확인)" if cancelled is False else "")
    err = None if filled_total > 0 else "미체결"
    if cancelled is False:
        err = (err + " · " if err else "") + "잔량 취소 실패"
    return {"filled": filled_total > 0, "filled_qty": filled_total, "px": avg, "order_no": odno,
            "simulated": False, "resp": resp, "error": err, "target": qty,
            "cancel_failed": cancelled is False}


def _cancel(mode: str, b: KISBroker, code: str, resp: dict[str, Any]) -> bool:
    """살아있을지 모르는 잔량 취소. 실패면 뒤늦은 체결이 유령 잔량이 될 수 있어 반드시 드러낸다."""
    try:
        ok = bool(b.cancel_order(resp))
    except Exception as e:  # noqa: BLE001
        logger.warning("[{}] {} 잔량 취소 예외 — {}", mode, code, e)
        ok = False
    if not ok:
        logger.error("[{}] {} 잔량 취소 실패 — 미체결 잔량이 뒤늦게 체결될 수 있다. HTS 확인 필요", mode, code)
    return ok


def cancel(mode: str, order: dict[str, Any], broker: KISBroker | None = None) -> bool:
    if mode == "dryrun" or order.get("simulated") or not order.get("resp"):
        return True
    _check_env(mode)
    own = broker is None
    b = broker or KISBroker()
    try:
        return b.cancel_order(order["resp"])
    finally:
        if own:
            b.close()


def broker_positions(mode: str, broker: KISBroker | None = None) -> dict[str, int]:
    """계좌 잔고 {code: qty}. dryrun 은 빈 dict (크래시 복구는 DB 만 본다)."""
    if mode == "dryrun":
        return {}
    _check_env(mode)
    own = broker is None
    b = broker or KISBroker()
    try:
        out = {}
        for r in b.get_positions():
            q = int(float(r.get("hldg_qty") or 0))
            if q > 0:
                out[str(r.get("pdno"))] = q
        return out
    finally:
        if own:
            b.close()
