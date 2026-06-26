"""두 봇(스톡봇·대장주봇) 간 종목 점유 조정 — 공유 파일 + flock test-and-set.

목적
----
대장주봇이 핵심 종목(settings.symbols)을 매매할 수 있게 풀어주되(own-symbol priority),
**한 종목은 한 시점에 한 봇만** 점유하도록 보장한다. 그래야:
  · 더블 매수 방지 (두 봇이 같은 종목 동시 매수 → 리스크/자본 충돌)
  · 청산 주체 고정 (잡은 봇이 익절/손절까지 전담, 다른 봇은 그 종목에 손 안 댐)
  · 손익 귀속 명확화 (독점 구간이라 그 종목 체결은 전부 그 봇 것 → strategy 태그로 분리 가능)

substrate
---------
data/coord/owner.json 한 파일을 두 컨테이너가 같은 호스트 inode 로 공유한다
(KIS 유량 게이트 파일락과 동일 방식 — cross-process flock 검증됨).
fcntl 은 Linux 전용(Pi/Docker). 로컬(Windows)에선 no-op 폴백 — 두 봇 동시 구동은 Pi 뿐이라 무해.

원장 스키마
-----------
    { "000660": {"owner": "leader", "since": 1750.., "qty": 1, "confirmed": true}, ... }
  owner      : "leader" | "stock"
  since      : 점유 시각 (epoch sec) — grace 판정용
  qty        : 점유 시 수량 (참고용)
  confirmed  : 브로커 잔고에 실제로 잡힌 적이 있는지 (고아청소 판정용)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from loguru import logger

try:
    import fcntl  # Linux 전용
except ImportError:  # pragma: no cover - Windows 로컬
    fcntl = None  # type: ignore

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "data" / "coord"
_PATH = _DIR / "owner.json"

# 점유 직후 잔고 미반영 구간(시장가 체결 지연) — 이 동안은 미보유여도 고아청소 안 함.
_GRACE_SEC = 90.0


def _bare(code: object) -> str:
    return str(code or "").split(".")[0].strip()


def _open_fd() -> int:
    _DIR.mkdir(parents=True, exist_ok=True)
    return os.open(str(_PATH), os.O_RDWR | os.O_CREAT, 0o644)


def _read(fd: int) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 1 << 16).decode("utf-8", "ignore").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _write(fd: int, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.ftruncate(fd, len(payload))


def _with_lock(fn):
    """flock LOCK_EX 안에서 read→fn(data)->(data, result)→write 를 원자적으로 수행.

    fn 은 (data:dict) 를 받아 (새 data, 반환값) 을 돌려준다. 파일락 실패 시 OSError 전파.
    fcntl 없음(로컬)이면 락 없이 best-effort.
    """
    fd = _open_fd()
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = _read(fd)
            data, res = fn(data)
            _write(fd, data)
            return res
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def owner_of(symbol: object) -> str | None:
    """해당 종목의 현재 점유 봇("leader"/"stock") 또는 None."""
    code = _bare(symbol)

    def _fn(data):
        return data, (data.get(code) or {}).get("owner")

    try:
        return _with_lock(_fn)
    except OSError as exc:
        logger.warning("position_owner.owner_of 파일락 실패({}) — None 폴백", exc)
        return None


def claim(symbol: object, owner: str, qty: int = 0) -> bool:
    """비어있거나 이미 내 소유면 점유하고 True. 남(다른 봇)의 소유면 False.

    원자적 test-and-set — 두 봇이 같은 봉에 동시 진입 시도해도 한쪽만 성공한다.
    """
    code = _bare(symbol)

    def _fn(data):
        cur = data.get(code)
        if cur and cur.get("owner") not in (None, owner):
            return data, False
        keep_confirmed = bool(cur.get("confirmed")) if cur else False
        data[code] = {
            "owner": owner,
            "since": time.time(),
            "qty": int(qty),
            "confirmed": keep_confirmed,
        }
        return data, True

    try:
        return _with_lock(_fn)
    except OSError as exc:
        logger.warning("position_owner.claim 파일락 실패({}) — 점유 거부", exc)
        return False


def release(symbol: object, owner: str) -> None:
    """내 소유일 때만 점유 해제 (남의 소유면 건드리지 않음)."""
    code = _bare(symbol)

    def _fn(data):
        cur = data.get(code)
        if cur and cur.get("owner") == owner:
            data.pop(code, None)
        return data, None

    try:
        _with_lock(_fn)
    except OSError as exc:
        logger.warning("position_owner.release 파일락 실패({})", exc)


def reconcile(owner: str, held_codes) -> None:
    """owner 소유 항목을 실제 보유(held_codes)와 대조해 정합 + 고아 청소.

    adopt(점유 등록):
      · 실제 보유 중인데 원장에 없음/주인없음 → owner 로 등록(confirmed=True).
        전날 넘어온 포지션을 원장에 올려, 다른 봇이 그 종목을 못 잡게(더블 보유 방지)
        하면서도 자기 자신은 게이트에 안 걸려 매도·관리를 계속할 수 있게 한다.
      · 이미 내 소유면 confirmed=True 로 마킹(유지).
      · 남(다른 봇)의 소유면 건드리지 않음.
    cleanup(고아 청소) — 내 소유인데 더 이상 보유 안 함:
      · confirmed(=과거 잡혔었음) → 청산된 것 → 해제
      · 미confirmed + grace 이내 → 체결 대기로 보고 유지
      · 미confirmed + grace 초과 → 안 잡힌 스테일 점유 → 해제
    """
    held = {_bare(c) for c in held_codes}
    now = time.time()

    def _fn(data):
        # adopt — 실제 보유 종목을 원장에 반영
        for code in held:
            rec = data.get(code)
            if rec is None or rec.get("owner") is None:
                data[code] = {"owner": owner, "since": now, "qty": 0, "confirmed": True}
            elif rec.get("owner") == owner and not rec.get("confirmed"):
                rec["confirmed"] = True
                data[code] = rec
            # 남의 소유면 손대지 않음(상호배제 유지)
        # cleanup — 내 소유인데 보유 목록에 없는 고아 점유
        for code in list(data.keys()):
            rec = data.get(code) or {}
            if rec.get("owner") != owner or _bare(code) in held:
                continue
            if rec.get("confirmed"):
                data.pop(code, None)          # 잡혔다가 사라짐 = 청산 완료
            elif now - float(rec.get("since", 0)) >= _GRACE_SEC:
                data.pop(code, None)          # grace 지나도 안 잡힘 = 스테일
        return data, None

    try:
        _with_lock(_fn)
    except OSError as exc:
        logger.warning("position_owner.reconcile 파일락 실패({})", exc)
