"""Prometheus 메트릭.

`settings.metrics_port > 0` 이면 HTTP 서버를 띄운다.
Grafana 에서 Prometheus 데이터소스로 바라볼 수 있다.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, start_http_server
from loguru import logger

from stock_bot.config import settings

orders_total = Counter(
    "stock_bot_orders_total", "Total orders sent", ["symbol", "side", "mode"]
)
tick_errors_total = Counter("stock_bot_tick_errors_total", "Tick execution errors", ["symbol"])
last_price = Gauge("stock_bot_last_price", "Last observed price", ["symbol"])
position_qty = Gauge("stock_bot_position_qty", "Position quantity", ["symbol"])
position_avg_price = Gauge("stock_bot_position_avg_price", "Position average price", ["symbol"])

_started = False


def start_metrics_server() -> None:
    global _started
    if _started or settings.metrics_port <= 0:
        return
    start_http_server(settings.metrics_port)
    _started = True
    logger.info("metrics server on :{}", settings.metrics_port)
