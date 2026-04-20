"""WebSocket 체결 응답 파서."""
from __future__ import annotations

from stock_bot.broker.kis_ws import _parse_tick


def test_parse_tick_ok():
    # 필드: 종목코드^체결시각^현재가^...(12번째는 체결량)
    body = "^".join(
        ["005930", "093000", "70000", "0", "0", "0", "0", "0", "0", "0", "0", "0", "10"]
    )
    raw = f"0|H0STCNT0|001|{body}"
    tick = _parse_tick(raw)
    assert tick is not None
    assert tick.symbol == "005930"
    assert tick.price == 70000.0
    assert tick.time == "093000"
    assert tick.volume == 10


def test_parse_tick_non_data_returns_none():
    assert _parse_tick("1|PINGPONG|...") is None
    assert _parse_tick("garbage") is None
