"""backtrader 로 이동평균 크로스 전략 백테스트."""
from __future__ import annotations

from datetime import datetime

import backtrader as bt
import yfinance as yf
from loguru import logger


class MACrossStrategy(bt.Strategy):
    params = (
        ("short_window", 5),
        ("long_window", 20),
        ("stop_loss_pct", 5.0),
    )

    def __init__(self) -> None:
        self.short_ma = bt.indicators.SMA(self.data.close, period=self.p.short_window)
        self.long_ma = bt.indicators.SMA(self.data.close, period=self.p.long_window)
        self.crossover = bt.indicators.CrossOver(self.short_ma, self.long_ma)
        self.entry_price: float | None = None

    def next(self) -> None:
        if not self.position:
            if self.crossover[0] > 0:
                size = int(self.broker.cash * 0.95 // self.data.close[0])
                if size > 0:
                    self.buy(size=size)
                    self.entry_price = float(self.data.close[0])
        else:
            price = float(self.data.close[0])
            if self.entry_price is not None:
                loss_pct = (price - self.entry_price) / self.entry_price * 100
                if loss_pct <= -abs(self.p.stop_loss_pct):
                    self.close()
                    self.entry_price = None
                    return
            if self.crossover[0] < 0:
                self.close()
                self.entry_price = None


def run_backtest(
    symbol: str,
    start: str = "2022-01-01",
    end: str | None = None,
    cash: float = 10_000_000,
    short_window: int = 5,
    long_window: int = 20,
    stop_loss_pct: float = 5.0,
) -> dict[str, float]:
    """yfinance 로 과거 데이터 받아 백테스트 실행.

    국내 종목은 yfinance 티커에 `.KS` 를 붙인다 (예: 005930.KS).
    """
    end = end or datetime.now().strftime("%Y-%m-%d")
    df = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"no data for {symbol}")

    cerebro = bt.Cerebro()
    cerebro.broker.set_cash(cash)
    cerebro.broker.setcommission(commission=0.00015)
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(
        MACrossStrategy,
        short_window=short_window,
        long_window=long_window,
        stop_loss_pct=stop_loss_pct,
    )
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")

    start_value = cerebro.broker.getvalue()
    results = cerebro.run()
    end_value = cerebro.broker.getvalue()
    strat = results[0]

    summary = {
        "symbol": symbol,
        "start_cash": start_value,
        "end_cash": end_value,
        "return_pct": (end_value - start_value) / start_value * 100,
        "sharpe": strat.analyzers.sharpe.get_analysis().get("sharperatio") or 0.0,
        "max_drawdown_pct": strat.analyzers.dd.get_analysis().get("max", {}).get("drawdown", 0.0),
    }
    logger.info("backtest {}: {}", symbol, summary)
    return summary


if __name__ == "__main__":
    import sys

    sym = sys.argv[1] if len(sys.argv) > 1 else "005930.KS"
    print(run_backtest(sym))
