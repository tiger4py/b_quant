# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import rolling_high, rolling_low

META = {
    "id": "kdj_cross",
    "name": "KDJ 低位金叉",
    "description": "K线上穿D线且K低于35买入，K下穿D线且K高于65卖出。",
}


def generate_signals(bars):
    highs = rolling_high([b["high"] for b in bars], 9)
    lows = rolling_low([b["low"] for b in bars], 9)
    k_values = []
    d_values = []
    k = 50
    d = 50
    for i, bar in enumerate(bars):
        if highs[i] is None or lows[i] is None or highs[i] == lows[i]:
            k_values.append(None)
            d_values.append(None)
            continue
        rsv = (bar["close"] - lows[i]) / (highs[i] - lows[i]) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        k_values.append(k)
        d_values.append(d)

    signals = []
    for i in range(1, len(bars)):
        if (
            k_values[i - 1] is None
            or d_values[i - 1] is None
            or k_values[i] is None
            or d_values[i] is None
        ):
            continue
        if k_values[i - 1] <= d_values[i - 1] and k_values[i] > d_values[i] and k_values[i] < 35:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "KDJ 低位金叉"})
        elif k_values[i - 1] >= d_values[i - 1] and k_values[i] < d_values[i] and k_values[i] > 65:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "KDJ 高位死叉"})
    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
