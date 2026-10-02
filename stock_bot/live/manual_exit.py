"""수동 청산 명령 큐 — 웹에서 넣은 '전량 매도' / '진입가 대비 N% 손절' 을 각 봇이 집행.

흐름
----
웹(stock-web) 이 data/coord/manual_exit.json 에 종목별 명령을 쓰고, 그 종목을 실제로
들고 있는 봇이 자기 청산 경로로 집행한다. 어느 봇 소유인지 웹이 정하지 않는다 —
봇마다 '내가 관리하는 종목'에 걸린 명령만 집는다(claim 은 flock test-and-set 이라
두 봇이 같은 명령을 동시에 집행할 수 없다).

  · 대장주봇 : _manage_position → st["force_exit"] 로 넘겨 기존 강제청산 경로 사용
  · 스윙봇   : _eod_timer(5초) → SwingLive._exit
  · 단타봇   : 계좌 잔고 중 대장주·스윙 소유가 아닌 전부(설정 종목 밖 보유분 포함)

명령 종류
---------
  sell : 즉시 전량 시장가 매도. 발행 당일에만 유효(다음 날 자동 만료).
  stop : 진입가(봇이 아는 평단) × (1 + pct/100) 이하가 되면 전량 매도. pct 는 음수(-3 = -3%).
         봇 자체 손절·익절은 그대로 살아 있고, 이것은 '추가' 트리거다(봇 손절을 대체하지 않음).
         취소하거나 집행될 때까지 유지. 계좌에서 종목이 사라지면 단타봇 틱이 정리한다.

스키마
------
    {"orders": {"005930": {"code", "action", "pct", "ts", "date", "status", "by", "msg"}},
     "history": [ ...최근 종료 명령... ]}
  status : pending(sell 대기) / active(stop 감시중) / running(집행중) /
           done / error / expired / canceled  — 뒤의 넷은 history 로 이동
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from stock_bot.market_calendar import KST as _KST

try:
    import fcntl  # Linux 전용
except ImportError:  # pragma: no cover - Windows 로컬
    fcntl = None  # type: ignore

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "data" / "coord"
_PATH = _DIR / "manual_exit.json"

_HISTORY_MAX = 50
# 손절 % 허용 범위 — 오타(-30 을 -3 으로, 3 을 -3 으로) 방지용 안전 범위.
PCT_MIN, PCT_MAX = -30.0, 30.0
# 미보유 정리 유예 — 명령 직후 잔고 조회 지연·부분체결로 잠깐 안 보일 수 있다.
_CLEAN_GRACE_SEC = 120.0

_LIVE = ("pending", "active", "running")


def _bare(code: object) -> str:
    return str(code or "").split(".")[0].strip()


def _today() -> str:
    return datetime.now(tz=_KST).strftime("%Y-%m-%d")


def _open_fd() -> int:
    _DIR.mkdir(parents=True, exist_ok=True)
    return os.open(str(_PATH), os.O_RDWR | os.O_CREAT, 0o666)


def _read(fd: int) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        b = os.read(fd, 1 << 16)
        if not b:
            break
        chunks.append(b)
    raw = b"".join(chunks).decode("utf-8", "ignore").strip()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("orders", {})
    data.setdefault("history", [])
    return data


def _write(fd: int, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.ftruncate(fd, len(payload))


def _with_lock(fn):
    fd = _open_fd()
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = _read(fd)
            data, res = fn(data)
            if data is not None:
                _write(fd, data)
            return res
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _retire(data: dict, code: str, status: str, msg: str = "", by: str = "") -> dict | None:
    rec = data["orders"].pop(code, None)
    if rec is None:
        return None
    rec = dict(rec)
    rec["status"] = status
    rec["msg"] = msg or rec.get("msg", "")
    if by:
        rec["by"] = by
    rec["closed_ts"] = time.time()
    data["history"] = ([rec] + list(data.get("history") or []))[:_HISTORY_MAX]
    return rec


def _expire_stale(data: dict) -> bool:
    """발행일이 지난 sell 명령은 만료(장 마감 후 넣은 명령이 다음 날 아침 시장가로 나가지 않게)."""
    today = _today()
    stale = [c for c, r in data["orders"].items()
             if r.get("action") == "sell" and r.get("status") == "pending" and r.get("date") != today]
    for c in stale:
        _retire(data, c, "expired", "발행일 경과 — 미집행 만료")
    return bool(stale)


# ── 웹(발행) 쪽 ─────────────────────────────────────────────────────────────


def submit(code: object, action: str, pct: float | None = None, source: str = "web") -> dict:
    """명령 등록. 같은 종목의 기존 대기 명령은 대체(canceled 로 history 이동).

    집행 중(running) 인 명령이 있으면 거부 — 매도가 나가는 중에 손절선을 바꾸면 의미가 없다.
    """
    code = _bare(code)
    if not code.isdigit() or len(code) != 6:
        raise ValueError("종목코드는 6자리 숫자")
    if action not in ("sell", "stop"):
        raise ValueError("action 은 sell / stop")
    if action == "stop":
        if pct is None:
            raise ValueError("손절 % 가 필요합니다")
        pct = float(pct)
        if not (PCT_MIN <= pct <= PCT_MAX):
            raise ValueError(f"손절 % 는 {PCT_MIN:g} ~ {PCT_MAX:g} 범위")
    else:
        pct = None

    def _fn(data):
        _expire_stale(data)
        cur = data["orders"].get(code)
        if cur and cur.get("status") == "running":
            raise ValueError("이 종목은 매도 집행 중입니다")
        if cur:
            _retire(data, code, "canceled", "새 명령으로 대체")
        rec = {
            "code": code, "action": action, "pct": pct,
            "ts": time.time(), "date": _today(),
            "status": "pending" if action == "sell" else "active",
            "by": "", "msg": "", "source": source,
        }
        data["orders"][code] = rec
        return data, dict(rec)

    rec = _with_lock(_fn)
    logger.info("수동청산 명령 등록 {} {} {}", code, action, "" if pct is None else f"{pct:+g}%")
    return rec


def cancel(code: object) -> dict | None:
    code = _bare(code)

    def _fn(data):
        cur = data["orders"].get(code)
        if not cur:
            return None, None
        if cur.get("status") == "running":
            raise ValueError("이미 매도 집행 중이라 취소할 수 없습니다")
        return data, _retire(data, code, "canceled", "사용자 취소")

    return _with_lock(_fn)


def snapshot() -> dict:
    """{"orders": {...}, "history": [...]} 사본 (만료 처리 포함)."""
    def _fn(data):
        changed = _expire_stale(data)
        return (data if changed else None), json.loads(json.dumps(data))

    try:
        return _with_lock(_fn)
    except OSError as exc:
        logger.warning("manual_exit.snapshot 파일락 실패({})", exc)
        return {"orders": {}, "history": []}


# ── 봇(집행) 쪽 ─────────────────────────────────────────────────────────────


def stop_price(entry: float, pct: float) -> float:
    return float(entry) * (1 + float(pct) / 100.0)


def peek(code: object) -> dict | None:
    """락만 잡고 읽기 — 해당 종목에 살아있는 명령(사본) 또는 None."""
    code = _bare(code)

    def _fn(data):
        rec = data["orders"].get(code)
        if rec and rec.get("action") == "sell" and rec.get("date") != _today():
            return None, None  # 만료 대상 — 집행하지 않음(정리는 snapshot/submit 이)
        return None, (dict(rec) if rec else None)

    try:
        return _with_lock(_fn)
    except OSError as exc:
        logger.warning("manual_exit.peek 파일락 실패({}) — 명령 없음으로 처리", exc)
        return None


def claim(code: object, by: str, entry: float, price: float) -> str | None:
    """집행할 명령이 있으면 running 으로 바꾸고 청산 사유 문자열을 반환, 없으면 None.

    sell → 무조건 집행. stop → price <= entry*(1+pct/100) 일 때만 집행.
    원자적 test-and-set 이라 두 봇이 같은 명령을 동시에 집을 수 없다.
    """
    code = _bare(code)

    def _fn(data):
        rec = data["orders"].get(code)
        if not rec or rec.get("status") not in ("pending", "active"):
            return None, None
        if rec.get("action") == "sell":
            if rec.get("date") != _today():
                return None, None
            reason = "수동청산(전량)"
        else:
            if not entry or entry <= 0 or not price or price <= 0:
                return None, None
            sp = stop_price(entry, rec["pct"])
            if price > sp:
                return None, None
            reason = f"수동손절({rec['pct']:+g}%)"
            rec["trigger_px"] = float(price)
            rec["stop_px"] = round(sp, 2)
        rec["status"] = "running"
        rec["by"] = by
        rec["run_ts"] = time.time()
        return data, reason

    try:
        reason = _with_lock(_fn)
    except OSError as exc:
        logger.warning("manual_exit.claim 파일락 실패({})", exc)
        return None
    if reason:
        logger.info("수동청산 집행 시작 {} by={} — {}", code, by, reason)
    return reason


def finish(code: object, ok: bool, msg: str = "", by: str = "") -> None:
    """집행 결과 기록. ok=False 면 error 로 종료(재시도는 사용자가 다시 명령)."""
    code = _bare(code)

    def _fn(data):
        rec = data["orders"].get(code)
        if not rec:
            return None, None
        return data, _retire(data, code, "done" if ok else "error", msg, by)

    try:
        _with_lock(_fn)
    except OSError as exc:
        logger.warning("manual_exit.finish 파일락 실패({})", exc)


def release(code: object, msg: str = "") -> None:
    """running 을 되돌린다(주문 전 단계에서 실패 — 다음 주기에 다시 집행)."""
    code = _bare(code)

    def _fn(data):
        rec = data["orders"].get(code)
        if not rec or rec.get("status") != "running":
            return None, None
        rec["status"] = "pending" if rec.get("action") == "sell" else "active"
        rec["msg"] = msg
        return data, None

    try:
        _with_lock(_fn)
    except OSError:
        pass


def clean_unheld(held_codes) -> list[str]:
    """계좌에 없는 종목의 대기 명령 정리 — 다른 날 같은 종목 재매수에 묵은 손절선이 붙지 않게.

    계좌 전체 잔고를 보는 단타봇 틱이 호출한다. 집행 중(running) 명령은 건드리지 않는다.
    """
    held = {_bare(c) for c in held_codes}
    now = time.time()

    def _fn(data):
        gone = [c for c, r in data["orders"].items()
                if c not in held and r.get("status") in ("pending", "active")
                and now - float(r.get("ts") or 0) >= _CLEAN_GRACE_SEC]
        for c in gone:
            _retire(data, c, "canceled", "계좌에 보유 없음 — 자동 정리")
        return (data if gone else None), gone

    try:
        return _with_lock(_fn) or []
    except OSError:
        return []


def has_orders() -> bool:
    """살아있는 명령이 하나라도 있는지 — 없으면 단타봇 감시 잡이 KIS 를 부르지 않는다."""
    def _fn(data):
        return None, any(r.get("status") in _LIVE for r in data["orders"].values())

    try:
        return bool(_with_lock(_fn))
    except OSError:
        return False
