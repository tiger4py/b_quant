# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import atr, rolling_high

META = {
    "id": "atr_trailing",
    "name": "ATR 突破跟踪",
    "description": "突破20日高点买入，收盘跌破入场后最高价减3倍ATR卖出。",
}


def generate_signals(bars):
    highs = rolling_high([b["high"] for b in bars], 20)
    atr14 = atr(bars, 14)
    signals = []
    in_pos = False
    peak = None
    for i in range(1, len(bars)):
        if highs[i - 1] is None or atr14[i] is None:
            continue
        close = bars[i]["close"]
        if not in_pos and close > highs[i - 1]:
            in_pos = True
            peak = bars[i]["high"]
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "ATR 趋势突破买入"})
        elif in_pos:
            peak = max(peak, bars[i]["high"])
            stop = peak - 3 * atr14[i]
            if close < stop:
                in_pos = False
                peak = None
                signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "跌破 ATR 跟踪止损"})
    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
