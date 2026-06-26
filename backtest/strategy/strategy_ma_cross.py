# -*- coding: utf-8 -*-
"""MA5/MA20 金叉 + 40天延迟确认：涨了才买，死叉卖出"""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma

META = {
    "id": "ma_cross",
    "name": "MA5/MA20 延迟确认",
    "description": "MA5上穿MA20后等40天，涨了才买，死叉卖出。不涨就跳过。",
}

# ============ 可调参数 ============
MA_FAST = 5
MA_SLOW = 20
CONFIRM_DAYS = 40            # 金叉后等多少天才确认


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    ma5 = sma(closes, MA_FAST)
    ma20 = sma(closes, MA_SLOW)

    signals = []
    in_pos = False
    entry_price = None
    pending = None  # 等待确认的买入候选

    for i in range(1, len(bars)):
        if ma5[i - 1] is None or ma20[i - 1] is None:
            continue

        # ==== 检测金叉信号，记录候选 ====
        if not in_pos and pending is None:
            if ma5[i - 1] <= ma20[i - 1] and ma5[i] > ma20[i]:
                pending = {
                    "signal_index": i,
                    "signal_price": closes[i],
                    "signal_date": bars[i]["trade_date"],
                }

        # ==== 确认窗口：等 CONFIRM_DAYS 天后检查 ====
        if pending is not None and not in_pos:
            days_passed = i - pending["signal_index"]
            if days_passed >= CONFIRM_DAYS:
                if closes[i] > pending["signal_price"]:
                    # 40天后涨了 → 确认买入
                    in_pos = True
                    entry_price = closes[i]
                    signals.append({
                        "date": bars[i]["trade_date"],
                        "action": "buy",
                        "reason": f"金叉确认(40天涨{(closes[i]/pending['signal_price']-1)*100:.1f}%)",
                    })
                # 不管确认与否，清除pending
                pending = None

        # ==== 卖出：死叉 ====
        if in_pos:
            if ma5[i - 1] >= ma20[i - 1] and ma5[i] < ma20[i]:
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "sell",
                    "reason": "MA5 下穿 MA20",
                })
                in_pos = False
                entry_price = None

    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
