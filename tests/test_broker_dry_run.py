"""dry-run 모드에서는 네트워크 호출 없이 로그만 남아야 한다."""
from __future__ import annotations

from stock_bot.broker import KISBroker
from stock_bot.config import settings


def test_dry_run_returns_stub(monkeypatch):
    monkeypatch.setattr(settings, "trade_dry_run", True)
    broker = KISBroker()
    # 네트워크 호출이 발생하면 토큰이 필요해 실패한다. dry-run 에서는 호출되지 않아야 한다.
    monkeypatch.setattr(
        broker, "_ensure_token", lambda: (_ for _ in ()).throw(AssertionError("must not be called"))
    )
    result = broker.place_order("005930", "buy", 1)
    assert result == {"dry_run": True, "side": "buy", "symbol": "005930", "qty": 1}
    broker.close()
