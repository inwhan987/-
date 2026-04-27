from .compare import print_table, run_compare
from .engine import BacktestResult, run_strategy
from .runner import run_backtest  # 기존 backtrader 기반 (하위호환)

__all__ = [
    "run_backtest",
    "run_compare",
    "print_table",
    "run_strategy",
    "BacktestResult",
]
