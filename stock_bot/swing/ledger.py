# -*- coding: utf-8 -*-
"""스윙봇 ↔ 공용 원장 연결 — 거래 기록(trades.db) + 종목 점유(position_owner).

왜 필요한가
-----------
· 거래 기록: 대시보드 거래기록·매수이유·손익 분리는 stock_bot.storage.db.TradeLog 를 본다.
  스윙 체결을 여기에 strategy="swing_<전략>" 으로 남겨야 📈 스윙 배지·손익 귀속이 된다.
· 점유: 스톡봇 reconcile("stock") 은 주인 없는 계좌 보유분을 자기 것으로 흡수한다.
  스윙이 paper/live 로 산 종목을 먼저 "swing" 으로 점유해 두지 않으면 스톡봇이
  그 종목을 자기 포지션으로 보고 청산할 수 있다.

원칙
----
· dryrun(가상 체결) 은 기록만 남기고 점유는 하지 않는다 — 계좌에 실제 없는 종목을
  잡아 두면 다른 봇의 실매매를 막는다. 기록 reason 앞에 [dryrun] 을 붙여 구분.
· 원장 실패는 절대 매매 흐름을 막지 않는다 — 전부 try/except + 로그.
· TradeLog 는 cwd 상대 sqlite(trades.db). 스윙 컨테이너는 working_dir=/app 이라
  TRADES_DB_URL(docker-compose) 로 /app/db/trades.db 를 가리킨다.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

OWNER = "swing"


def strategy_tag(pos: dict) -> str:
    return f"swing_{str(pos.get('strategy') or '').upper() or 'NA'}"


def _simulated(mode: str, res: dict | None) -> bool:
    return mode == "dryrun" or bool((res or {}).get("simulated"))


def record(mode: str, pos: dict, side: str, qty: int, px: float, reason: str,
           res: dict | None = None) -> None:
    """체결 1건을 TradeLog 에 기록. 실패해도 예외를 밖으로 내지 않는다."""
    try:
        from stock_bot.storage.db import init_db, record_trade
        init_db()
        sim = _simulated(mode, res)
        resp = (res or {}).get("resp")
        record_trade(
            symbol=str(pos["code"]), side=side, quantity=int(qty), price=float(px),
            reason=("[dryrun] " if sim else "") + reason,
            broker_response=json.dumps(resp, ensure_ascii=False)[:500] if resp else "",
            strategy=strategy_tag(pos),
            details={
                "bot": OWNER, "mode": mode, "simulated": sim,
                "order_no": (res or {}).get("order_no"),
                "stop": pos.get("stop_px"), "tp": pos.get("tp_px"),
                "entry_px": pos.get("entry_px"), "signal_date": pos.get("signal_date"),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("swing ledger 기록 실패 {} {} x{}: {}", side, pos.get("code"), qty, e)


def claim(mode: str, code: str, qty: int) -> bool:
    """주문 전 점유 선점. dryrun 은 항상 True. 남(스톡봇·대장주)이 잡고 있으면 False."""
    if mode == "dryrun":
        return True
    try:
        from stock_bot.live import position_owner
        return position_owner.claim(code, OWNER, qty)
    except Exception as e:  # noqa: BLE001
        logger.warning("swing ledger 점유 실패 {}: {} — 진입 보류", code, e)
        return False


def release(mode: str, code: str) -> None:
    if mode == "dryrun":
        return
    try:
        from stock_bot.live import position_owner
        position_owner.release(code, OWNER)
    except Exception as e:  # noqa: BLE001
        logger.warning("swing ledger 점유 해제 실패 {}: {}", code, e)


def reconcile(mode: str, held_codes: list[str]) -> None:
    """시작 시 보유 목록과 원장 정합(고아 점유 청소·전날 보유분 등록)."""
    if mode == "dryrun":
        return
    try:
        from stock_bot.live import position_owner
        position_owner.reconcile(OWNER, held_codes)
    except Exception as e:  # noqa: BLE001
        logger.warning("swing ledger reconcile 실패: {}", e)


def shared_used(mode: str) -> int | None:
    """공용 슬롯 사용 수(원장 owner∈{stock,swing}). dryrun 은 원장을 안 쓰므로 스톡봇 몫만 돌려준다
    (호출측이 자기 가상 보유를 더한다). 원장 실패 시 None."""
    try:
        from stock_bot.live import position_owner
        if mode == "dryrun":
            return position_owner.count_owned(("stock",))
        return position_owner.count_owned(("stock", "swing"))
    except Exception as e:  # noqa: BLE001
        logger.warning("swing ledger 공용 슬롯 조회 실패: {}", e)
        return None


def owner_of(code: str) -> str | None:
    try:
        from stock_bot.live import position_owner
        return position_owner.owner_of(code)
    except Exception:  # noqa: BLE001
        return None


def summary(res: dict | None) -> dict[str, Any]:
    return {k: (res or {}).get(k) for k in ("filled_qty", "px", "order_no", "simulated", "error")}
