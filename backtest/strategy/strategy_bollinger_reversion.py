# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma, stddev

META = {
    "id": "bollinger_reversion",
    "name": "布林带均值回归",
    "description": "收盘跌破布林下轨买入，回到中轨上方卖出。",
}


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    mid = sma(closes, 20)
    dev = stddev(closes, 20)
    signals = []
    for i in range(1, len(bars)):
        if mid[i - 1] is None or mid[i] is None or dev[i] is None:
            continue
        lower = mid[i] - 2 * dev[i]
        if closes[i - 1] >= lower and closes[i] < lower:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "收盘跌破布林下轨"})
        elif closes[i - 1] <= mid[i - 1] and closes[i] > mid[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "收盘回到布林中轨上方"})
    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
