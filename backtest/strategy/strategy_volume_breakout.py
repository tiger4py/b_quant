# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma

META = {
    "id": "volume_breakout",
    "name": "放量突破",
    "description": "收盘突破20日均线且成交量大于5日均量1.8倍买入，跌破20日均线卖出。",
}


# ============ 可调参数 ============
VOL_RATIO = 1.8
MA_PERIOD = 20


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    ma20 = sma(closes, MA_PERIOD)
    vol5 = sma(volumes, 5)
    signals = []
    for i in range(1, len(bars)):
        if ma20[i - 1] is None or ma20[i] is None or vol5[i] is None:
            continue
        if closes[i - 1] <= ma20[i - 1] and closes[i] > ma20[i] and volumes[i] > vol5[i] * VOL_RATIO:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "放量突破 MA20"})
        elif closes[i - 1] >= ma20[i - 1] and closes[i] < ma20[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "跌破 MA20"})
    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
