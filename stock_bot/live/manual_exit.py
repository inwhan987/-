"""수동 매도 명령 큐 — 웹에서 넣은 '보유 수량의 N% 매도'(100% = 전량) 를 각 봇이 집행.

흐름
----
웹(stock-web) 이 data/coord/manual_exit.json 에 종목별 명령을 쓰고, 그 종목을 실제로
들고 있는 봇이 자기 매도 경로로 집행한다. 어느 봇 소유인지 웹이 정하지 않는다 —
봇마다 '내가 관리하는 종목'에 걸린 명령만 집는다(claim 은 flock test-and-set 이라
두 봇이 같은 명령을 동시에 집행할 수 없다).

  · 대장주봇 : _manage_position — 전량은 st["force_exit"] 강제청산 경로, 일부는 _manual_partial_sell
  · 스윙봇   : _eod_timer(5초) — 전량은 SwingLive._exit, 일부는 SwingLive._sell_part
  · 단타봇   : 계좌 잔고 중 대장주·스윙 소유가 아닌 전부(설정 종목 밖 보유분 포함)

명령
----
  sell : 집행 시점 보유 수량 × pct% 를 시장가 매도(반올림, 최소 1주). pct=100 이면 전량.
         계산한 수량이 보유 수량 이상이면 전량 매도로 처리한다.
         발행 당일에만 유효(장 마감 후 넣은 명령은 다음 날 자동 만료).
         집행 전(pending)이면 같은 종목에 새 명령을 넣어 비율을 수정할 수 있다.

스키마
------
    {"orders": {"005930": {"code", "action", "pct", "ts", "date", "status", "by", "msg"}},
     "history": [ ...최근 종료 명령... ]}
  status : pending(대기) / running(집행중) / done / error / expired / canceled
           — 뒤의 넷은 history 로 이동
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
PCT_MIN, PCT_MAX = 1.0, 100.0
# 미보유 정리 유예 — 명령 직후 잔고 조회 지연·부분체결로 잠깐 안 보일 수 있다.
_CLEAN_GRACE_SEC = 120.0

_LIVE = ("pending", "running")


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


def _is_valid(rec: dict) -> bool:
    """현행 스키마의 sell 명령인지 — 예전 'stop'(진입가 대비 손절) 명령은 집행하지 않는다."""
    return rec.get("action") == "sell"


def _expire_stale(data: dict) -> bool:
    """발행일이 지난 대기 명령 만료(장 마감 후 넣은 명령이 다음 날 아침 시장가로 나가지 않게).

    폐지된 'stop' 명령도 여기서 정리한다.
    """
    today = _today()
    stale = [c for c, r in data["orders"].items()
             if r.get("status") != "running" and (not _is_valid(r) or r.get("date") != today)]
    for c in stale:
        _retire(data, c, "expired", "발행일 경과 — 미집행 만료" if _is_valid(data["orders"][c])
                else "폐지된 손절 명령 — 정리")
    return bool(stale)


def portion(held_qty: int, pct: float) -> int:
    """보유 held_qty 중 pct% 에 해당하는 매도 수량(반올림, 최소 1주, 최대 보유 수량)."""
    held_qty = int(held_qty)
    if held_qty <= 0:
        return 0
    if float(pct) >= 100:
        return held_qty
    return max(1, min(held_qty, int(held_qty * float(pct) / 100 + 0.5)))


def label(pct: float) -> str:
    return "수동매도(전량)" if float(pct) >= 100 else f"수동매도({float(pct):g}%)"


# ── 웹(발행) 쪽 ─────────────────────────────────────────────────────────────


def submit(code: object, pct: float = 100.0, source: str = "web") -> dict:
    """명령 등록·수정. 같은 종목의 대기 명령은 새 비율로 대체(canceled 로 history 이동).

    집행 중(running) 인 명령이 있으면 거부 — 주문이 나가는 중에 수량을 바꿀 수 없다.
    """
    code = _bare(code)
    if not code.isdigit() or len(code) != 6:
        raise ValueError("종목코드는 6자리 숫자")
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        raise ValueError("매도 비율(%)이 숫자가 아닙니다") from None
    if not (PCT_MIN <= pct <= PCT_MAX):
        raise ValueError(f"매도 비율은 {PCT_MIN:g} ~ {PCT_MAX:g}%")
    pct = round(pct, 1)

    def _fn(data):
        _expire_stale(data)
        cur = data["orders"].get(code)
        if cur and cur.get("status") == "running":
            raise ValueError("이 종목은 매도 집행 중입니다")
        if cur:
            _retire(data, code, "canceled", f"{pct:g}% 로 수정")
        rec = {
            "code": code, "action": "sell", "pct": pct,
            "ts": time.time(), "date": _today(),
            "status": "pending", "by": "", "msg": "", "source": source,
        }
        data["orders"][code] = rec
        return data, dict(rec)

    rec = _with_lock(_fn)
    logger.info("수동매도 명령 등록 {} {:g}%", code, pct)
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


def peek(code: object) -> dict | None:
    """락만 잡고 읽기 — 해당 종목에 집행 가능한 명령(사본) 또는 None."""
    code = _bare(code)

    def _fn(data):
        rec = data["orders"].get(code)
        if not rec or not _is_valid(rec) or rec.get("date") != _today():
            return None, None  # 만료·폐지 대상 — 집행하지 않음(정리는 snapshot/submit 이)
        return None, dict(rec)

    try:
        return _with_lock(_fn)
    except OSError as exc:
        logger.warning("manual_exit.peek 파일락 실패({}) — 명령 없음으로 처리", exc)
        return None


def claim(code: object, by: str) -> dict | None:
    """집행할 명령이 있으면 running 으로 바꾸고 {"pct", "reason"} 를 반환, 없으면 None.

    원자적 test-and-set 이라 두 봇이 같은 명령을 동시에 집을 수 없다.
    """
    code = _bare(code)

    def _fn(data):
        rec = data["orders"].get(code)
        if not rec or rec.get("status") != "pending" or not _is_valid(rec):
            return None, None
        if rec.get("date") != _today():
            return None, None
        rec["status"] = "running"
        rec["by"] = by
        rec["run_ts"] = time.time()
        pct = float(rec.get("pct") or 100)
        return data, {"pct": pct, "reason": label(pct)}

    try:
        res = _with_lock(_fn)
    except OSError as exc:
        logger.warning("manual_exit.claim 파일락 실패({})", exc)
        return None
    if res:
        logger.info("수동매도 집행 시작 {} by={} — {}", code, by, res["reason"])
    return res


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
    """running 을 pending 으로 되돌린다(한 주도 안 팔림 — 다음 주기에 다시 집행)."""
    code = _bare(code)

    def _fn(data):
        rec = data["orders"].get(code)
        if not rec or rec.get("status") != "running":
            return None, None
        rec["status"] = "pending"
        rec["msg"] = msg
        return data, None

    try:
        _with_lock(_fn)
    except OSError:
        pass


def clean_unheld(held_codes) -> list[str]:
    """계좌에 없는 종목의 대기 명령 정리.

    계좌 전체 잔고를 보는 단타봇 틱이 호출한다. 집행 중(running) 명령은 건드리지 않는다.
    """
    held = {_bare(c) for c in held_codes}
    now = time.time()

    def _fn(data):
        gone = [c for c, r in data["orders"].items()
                if c not in held and r.get("status") == "pending"
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
