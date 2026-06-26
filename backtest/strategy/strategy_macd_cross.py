# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import ema

META = {
    "id": "macd_cross",
    "name": "MACD 金叉死叉",
    "description": "DIF 上穿 DEA 买入，下穿卖出。",
}


# ============ 可调参数 ============
EMA_FAST = 12
EMA_SLOW = 26
SIGNAL = 9


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    dif = [a - b for a, b in zip(ema(closes, EMA_FAST), ema(closes, EMA_SLOW))]
    dea = ema(dif, SIGNAL)
    signals = []
    for i in range(1, len(bars)):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "DIF 上穿 DEA"})
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "DIF 下穿 DEA"})
    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
