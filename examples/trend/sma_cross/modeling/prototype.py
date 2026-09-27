"""A prototype of sma_cross, outside any trading engine.

Sketch the rule here in plain Python before writing it into
refinement/nautilus/strategy.py: load the parameters, compute the signal over
past bars, print what it would have done. Whatever this prints is a study, not a
result; the results that count come from `make backtest` and live in backtests/.
"""

from __future__ import annotations

from pathlib import Path

from custos_toolkit.config import load_config

STRATEGY_DIR = Path(__file__).resolve().parents[1]


def parameters() -> dict:
    """The `parameters` section of config.yaml, the values the strategy itself reads."""
    return dict(load_config(STRATEGY_DIR / "config.yaml").parameters or {})


def run() -> None:
    params = parameters()
    print(f"sma_cross prototype: parameters {params}")
    # Load bars, apply the rule and print the signals it would have produced.


if __name__ == "__main__":
    run()
