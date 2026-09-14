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

_FILL_ATTEMPTS = 5
_FILL_WAIT = 1.0


def _check_env(mode: str) -> None:
    if mode == "paper" and not settings.is_paper:
        raise SystemExit("SWING_MODE=paper 인데 KIS_ENV 가 real — 주문 중단")
    if mode == "live" and settings.is_paper:
        raise SystemExit("SWING_MODE=live 인데 KIS_ENV 가 paper — 주문 중단")


def place(mode: str, code: str, side: str, qty: int, px: float,
          broker: KISBroker | None = None) -> dict[str, Any]:
    """반환: {"filled": bool, "filled_qty": int, "px": 평균체결가, "order_no": str|None,
              "simulated": bool, "resp": 원응답|None, "error": str|None}

    filled 는 '전량 체결' 이 아니라 '1주 이상 체결' 이다. 부분체결이면 filled_qty
    로 실제 수량을 쓰고 잔량은 취소한다(호출측이 shares 를 이 값으로 바꾼다).
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
        try:
            resp = b.place_order(code, side, qty, 0.0, order_type="market")
        except OrderRejectedError as e:
            logger.warning("[{}] 주문 거부 {} {} x{}: {}", mode, side, code, qty, e)
            return {"filled": False, "filled_qty": 0, "px": 0.0, "order_no": None,
                    "simulated": False, "resp": None, "error": f"거부: {e}"}
        odno = str((resp.get("output") or {}).get("ODNO", "") or "")
        fill = b.get_order_fill(code, resp, attempts=_FILL_ATTEMPTS, wait=_FILL_WAIT)
        if fill is None:
            # 조회 실패 — 체결됐는지 모른다. 잔량 취소로 마감시키고 호출측에 '불명' 을 알린다
            b.cancel_order(resp)
            logger.error("[{}] 체결 조회 실패 {} {} — 잔량 취소, 판단 보류", mode, side, code)
            return {"filled": False, "filled_qty": 0, "px": 0.0, "order_no": odno,
                    "simulated": False, "resp": resp, "error": "체결조회실패"}
        fq = int(fill.get("filled_qty") or 0)
        rmn = fill.get("rmn_qty")
        if rmn is None or rmn > 0:
            b.cancel_order(resp)                       # 시장가 잔량도 호가창에 남는다
            time.sleep(0.5)
            again = b.get_order_fill(code, resp, attempts=2, wait=0.5)
            if again:
                fq = max(fq, int(again.get("filled_qty") or 0))
                fill = again
        avg = float(fill.get("avg_price") or 0.0) or float(px)
        logger.info("[{}] {} {} 주문 {} 체결 {}/{} @ {:,.0f}", mode, side, code, odno, fq, qty, avg)
        return {"filled": fq > 0, "filled_qty": fq, "px": avg, "order_no": odno,
                "simulated": False, "resp": resp,
                "error": None if fq > 0 else "미체결"}
    finally:
        if own:
            b.close()


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
