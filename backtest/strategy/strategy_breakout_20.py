# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import rolling_high, rolling_low

META = {
    "id": "breakout_20",
    "name": "20日突破",
    "description": "收盘突破前20日高点买入，跌破前10日低点卖出。",
}


# ============ 可调参数 ============
BREAKOUT_PERIOD = 20
STOPLOSS_PERIOD = 10


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    highs = rolling_high([b["high"] for b in bars], BREAKOUT_PERIOD)
    lows = rolling_low([b["low"] for b in bars], STOPLOSS_PERIOD)
    signals = []
    for i in range(1, len(bars)):
        if highs[i - 1] is None or lows[i - 1] is None:
            continue
        if closes[i] > highs[i - 1]:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "突破前20日高点"})
        elif closes[i] < lows[i - 1]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "跌破前10日低点"})
    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
