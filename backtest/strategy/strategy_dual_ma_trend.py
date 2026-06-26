# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma

META = {
    "id": "dual_ma_trend",
    "name": "双均线趋势过滤",
    "description": "10日均线在30日均线上方且收盘突破10日线买入，跌破30日线卖出。",
}


# ============ 可调参数 ============
MA_FAST = 10
MA_SLOW = 30


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    ma10 = sma(closes, MA_FAST)
    ma30 = sma(closes, MA_SLOW)
    signals = []
    for i in range(1, len(bars)):
        if ma10[i - 1] is None or ma10[i] is None or ma30[i - 1] is None or ma30[i] is None:
            continue
        if ma10[i] > ma30[i] and closes[i - 1] <= ma10[i - 1] and closes[i] > ma10[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "趋势向上且突破 MA10"})
        elif closes[i - 1] >= ma30[i - 1] and closes[i] < ma30[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "收盘跌破 MA30"})
    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
