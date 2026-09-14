# -*- coding: utf-8 -*-
"""KIS WebSocket 실시간 체결 클라이언트 — 재연결 + 분봉 합성 (명세 5-1, A안).

2026-09-14: kis_ws_swing.py 를 이 파일로 옮기고 기존 `stream_ticks` 는 호환용으로
남긴다(main.py `stream` 명령이 쓴다). 장중 6시간 30분을 버티기 위해:

  1. PINGPONG 응답      — 무시하면 서버가 연결을 끊는다
  2. 등록 ack 검증      — rt_cd 확인. 한도 초과(OPSP0008)를 조용히 넘기지 않는다
  3. 자동 재연결        — 지수 백오프(1→30초), 하루 재시도 상한
  4. 재연결 후 재등록   — 구독은 연결과 함께 날아간다
  5. 공백 콜백          — 끊긴 구간을 알려줘 REST 백필을 유도한다
  6. 틱 → N분봉 합성    — 확정된 봉만 넘겨준다 (판정용 3분 + 저장용 1분)
  7. 장중 종목 교체     — subscribe()/unsubscribe() (실측: 해제·재등록 됨, 동시 41)

TR ID H0STCNT0 국내주식 실시간 체결. 응답은 `0|H0STCNT0|001|필드^필드^…`.
모의 ws://ops.koreainvestment.com:31000, 실전 :21000.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Iterable

import websockets
from loguru import logger

from stock_bot.broker.kis import KISBroker
from stock_bot.config import settings

WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"
WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
TR_TICK = "H0STCNT0"
MAX_SUBSCRIBE = 41             # 2026-09-14 실측: 42번째 등록에 OPSP0008

_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0
_MAX_RECONNECT_PER_DAY = 200
_IDLE_TIMEOUT = 120.0


@dataclass
class Tick:
    code: str
    time: str            # HHMMSS
    price: float
    vwap: float = 0.0    # 가중평균주가 — KIS 가 계산해 보낸다
    day_open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    vol: int = 0         # 체결량
    acml_vol: int = 0    # 누적거래량

    # 구 kis_ws.Tick 호환 (main.py stream 명령)
    @property
    def symbol(self) -> str:
        return self.code

    @property
    def volume(self) -> int:
        return self.vol


@dataclass
class Bar:
    code: str
    key: str             # 봉 시작 HHMM
    open: float
    high: float
    low: float
    close: float
    volume: int
    ticks: int
    vwap: float


def ws_url() -> str:
    return WS_URL_PAPER if settings.is_paper else WS_URL_REAL


_ws_url = ws_url  # 호환


def _payload(approval_key: str, symbol: str, register: bool = True) -> str:
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1" if register else "2",   # 1=등록 2=해제
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": TR_TICK, "tr_key": symbol}},
    })


def _subscribe_payload(approval_key: str, symbol: str, tr_id: str = TR_TICK) -> str:  # 호환
    return _payload(approval_key, symbol, True)


def parse_tick(raw: str) -> Tick | None:
    try:
        header, tr_id, _cnt, body = raw.split("|", 3)
    except ValueError:
        return None
    if header != "0" or tr_id != TR_TICK:
        return None
    p = body.split("^")
    if len(p) < 14:
        return None
    try:
        return Tick(code=p[0], time=p[1], price=float(p[2]),
                    vwap=float(p[6]) if p[6] else 0.0,
                    day_open=float(p[7]), day_high=float(p[8]), day_low=float(p[9]),
                    vol=int(p[12] or 0), acml_vol=int(p[13] or 0))
    except (ValueError, IndexError):
        return None


_parse_tick = parse_tick  # 호환


def parse_ack(raw: str) -> tuple[str, str]:
    """(rt_cd, msg). rt_cd '0' 이 성공."""
    try:
        j = json.loads(raw)
        b = j.get("body", {}) or {}
        return str(b.get("rt_cd", "?")), str(b.get("msg1", ""))[:80]
    except Exception:  # noqa: BLE001
        return "?", raw[:80]


class BarBuilder:
    """틱을 N분봉으로 묶는다. 봉이 넘어갈 때만 확정된 봉을 돌려준다."""

    def __init__(self, bar_sec: int = 180):
        self.bar_min = max(1, bar_sec // 60)
        self._cur: dict[str, Bar] = {}

    def _key(self, hhmmss: str) -> str:
        h, m = int(hhmmss[:2]), int(hhmmss[2:4])
        return f"{h:02d}{(m // self.bar_min) * self.bar_min:02d}"

    def current(self, code: str) -> Bar | None:
        return self._cur.get(code)

    def add(self, t: Tick) -> Bar | None:
        if len(t.time) < 6:
            return None
        k = self._key(t.time)
        cur = self._cur.get(t.code)
        done = None
        if cur is None or cur.key != k:
            done = cur                       # 이전 봉이 확정됨
            self._cur[t.code] = Bar(code=t.code, key=k, open=t.price, high=t.price,
                                    low=t.price, close=t.price, volume=t.vol,
                                    ticks=1, vwap=t.vwap)
        else:
            cur.high = max(cur.high, t.price)
            cur.low = min(cur.low, t.price)
            cur.close = t.price
            cur.volume += t.vol
            cur.ticks += 1
            cur.vwap = t.vwap
        return done

    def flush(self, code: str | None = None) -> list[Bar]:
        """미확정 봉을 강제로 내보낸다(장 마감 등)."""
        if code:
            b = self._cur.pop(code, None)
            return [b] if b else []
        out = list(self._cur.values())
        self._cur.clear()
        return out


@dataclass
class SwingTickStream:
    """재연결·재등록·분봉 합성을 맡는 스트림.

    on_bar       : 판정용 봉(bar_sec) 확정 시
    on_store_bar : 저장용 봉(store_bar_sec) 확정 시 (0 이면 안 만듦)
    on_tick      : 틱마다 (손절·익절 즉시 판정용)
    on_gap       : (끊긴 초, 종목들) — REST 백필 유도
    """
    codes: list[str]
    bar_sec: int = 180
    store_bar_sec: int = 60
    on_bar: Callable[[Bar], Awaitable[None]] | None = None
    on_store_bar: Callable[[Bar], Awaitable[None]] | None = None
    on_tick: Callable[[Tick], Awaitable[None]] | None = None
    on_gap: Callable[[float, list[str]], Awaitable[None]] | None = None
    stop_at: str = "153100"          # 이 시각 넘으면 종료 (HHMMSS)

    _builder: BarBuilder = field(init=False)
    _store_builder: BarBuilder | None = field(init=False, default=None)
    _pending: list[tuple[str, bool]] = field(default_factory=list, init=False)
    _key: str = field(default="", init=False)
    _last_rx: float = field(default=0.0, init=False)
    reconnects: int = field(default=0, init=False)
    registered: list[str] = field(default_factory=list, init=False)
    rejected: list[tuple[str, str]] = field(default_factory=list, init=False)
    connected: bool = field(default=False, init=False)

    def __post_init__(self):
        self.codes = list(dict.fromkeys(self.codes))
        self._builder = BarBuilder(self.bar_sec)
        if self.store_bar_sec and self.store_bar_sec != self.bar_sec:
            self._store_builder = BarBuilder(self.store_bar_sec)

    @property
    def builder(self) -> BarBuilder:
        return self._builder

    # ── 장중 종목 교체 ────────────────────────────────────────────
    def subscribe(self, code: str) -> None:
        """다음 루프에서 등록. 이미 있으면 무시."""
        if code not in self.codes:
            self.codes.append(code)
            self._pending.append((code, True))

    def unsubscribe(self, code: str) -> None:
        if code in self.codes:
            self.codes.remove(code)
            self._pending.append((code, False))
        self._builder.flush(code)
        if self._store_builder:
            self._store_builder.flush(code)

    async def _apply_pending(self, ws) -> None:
        while self._pending:
            code, reg = self._pending.pop(0)
            await ws.send(_payload(self._key, code, register=reg))
            raw = await self._recv_ack(ws)
            rt, msg = parse_ack(raw) if raw else ("?", "ack 타임아웃")
            if reg:
                if rt == "0":
                    self.registered.append(code)
                    logger.info("등록 {} ({}종목)", code, len(self.registered))
                else:
                    self.rejected.append((code, f"rt_cd={rt} {msg}"))
                    logger.warning("등록 거부 {}: {}", code, msg)
            else:
                if code in self.registered:
                    self.registered.remove(code)
                logger.info("해제 {} rt={} ({}종목)", code, rt, len(self.registered))

    # ── 등록 ─────────────────────────────────────────────────────
    async def _register(self, ws, key: str) -> None:
        """한 종목씩 등록하고 ack 를 확인한다. 한도를 넘으면 여기서 실패 응답이 온다."""
        self.registered.clear()
        self.rejected.clear()
        for code in list(self.codes):
            await ws.send(_payload(key, code, register=True))
            raw = await self._recv_ack(ws)
            if raw is None:
                self.rejected.append((code, "ack 타임아웃"))
                continue
            rt, msg = parse_ack(raw)
            if rt == "0":
                self.registered.append(code)
            else:
                self.rejected.append((code, f"rt_cd={rt} {msg}"))
        logger.info("등록 {}/{}건", len(self.registered), len(self.codes))
        if self.rejected:
            logger.warning("등록 거부 {}건 — 첫 3건: {}", len(self.rejected), self.rejected[:3])

    async def _recv_ack(self, ws, timeout: float = 5.0) -> str | None:
        """ack 한 건. PINGPONG 은 응답하고, 틱이 섞여 오면 처리 후 계속."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except (asyncio.TimeoutError, ValueError):
                return None
            if "PINGPONG" in raw:
                await ws.pong(raw)
                continue
            if raw.startswith("{"):
                return raw
            t = parse_tick(raw)          # 등록 도중 들어온 틱도 버리지 않는다
            if t:
                await self._handle(t)
        return None

    # ── 틱 처리 ──────────────────────────────────────────────────
    async def _handle(self, t: Tick) -> None:
        if t.code not in self.codes:
            return                       # 해제 직후 잔여 틱
        if self.on_tick:
            await self.on_tick(t)
        done = self._builder.add(t)
        if done and self.on_bar:
            await self.on_bar(done)
        if self._store_builder is not None:
            d2 = self._store_builder.add(t)
            if d2 and self.on_store_bar:
                await self.on_store_bar(d2)
        elif done and self.on_store_bar:
            await self.on_store_bar(done)

    async def _flush_all(self) -> None:
        for b in self._builder.flush():
            if self.on_bar:
                await self.on_bar(b)
        if self._store_builder is not None:
            for b in self._store_builder.flush():
                if self.on_store_bar:
                    await self.on_store_bar(b)

    # ── 메인 루프 ────────────────────────────────────────────────
    async def run(self) -> None:
        backoff = _BACKOFF_START
        while True:
            if self.reconnects >= _MAX_RECONNECT_PER_DAY:
                logger.error("재연결 {}회 초과 — 중단합니다. 원인을 확인하세요", _MAX_RECONNECT_PER_DAY)
                await self._flush_all()
                return
            if time.strftime("%H%M%S") >= self.stop_at:
                logger.info("장 종료 시각 도달 — 종료")
                await self._flush_all()
                return

            down_since = time.monotonic()
            try:
                await self._session()
                await self._flush_all()
                return                                # stop_at 정상 종료
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.connected = False
                self.reconnects += 1
                gap = time.monotonic() - down_since
                logger.warning("연결 끊김({}회차): {} {} — {:.0f}초 후 재접속",
                               self.reconnects, type(e).__name__, str(e)[:80], backoff)
                if self.on_gap:
                    await self.on_gap(gap + backoff, list(self.codes))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _session(self) -> None:
        """연결 1회분. 예외를 내면 run() 이 재접속한다."""
        b = KISBroker()
        try:
            key = b.get_approval_key()      # 만료됐으면 여기서 재발급된다
        finally:
            b.close()
        self._key = key
        self._pending.clear()               # 재연결 시 codes 전체를 다시 등록하므로

        url = ws_url()
        logger.info("ws 접속 {} ({}종목)", url, len(self.codes))
        async with websockets.connect(url, ping_interval=None, close_timeout=5) as ws:
            await self._register(ws, key)
            if not self.registered:
                raise RuntimeError("등록된 종목이 0건 — 한도 초과이거나 키 문제")
            self.connected = True
            self._last_rx = time.monotonic()

            while True:
                if time.strftime("%H%M%S") >= self.stop_at:
                    return
                if self._pending:
                    await self._apply_pending(ws)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    if time.monotonic() - self._last_rx > _IDLE_TIMEOUT:
                        raise RuntimeError(f"{_IDLE_TIMEOUT:.0f}초 무응답")
                    continue
                self._last_rx = time.monotonic()
                if "PINGPONG" in raw:
                    await ws.pong(raw)
                    continue
                if raw.startswith("{"):
                    continue
                t = parse_tick(raw)
                if t:
                    await self._handle(t)


# ── 호환 API ─────────────────────────────────────────────────────────
async def stream_ticks(symbols: Iterable[str]) -> AsyncIterator[Tick]:
    """심볼 목록을 구독하고 체결 틱을 비동기 이터레이터로 반환 (main.py stream 명령)."""
    q: asyncio.Queue[Tick] = asyncio.Queue()

    async def on_tick(t: Tick) -> None:
        await q.put(t)

    st = SwingTickStream([s.split(".")[0] for s in symbols], on_tick=on_tick, stop_at="235959")
    task = asyncio.create_task(st.run())
    try:
        while not task.done():
            try:
                yield await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
        if task.exception():
            raise task.exception()  # type: ignore[misc]
    finally:
        task.cancel()


async def _demo() -> None:
    async for tick in stream_ticks(settings.symbols):
        logger.info("tick {} {} {}", tick.symbol, tick.price, tick.time)


if __name__ == "__main__":
    asyncio.run(_demo())
