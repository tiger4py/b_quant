# -*- coding: utf-8 -*-
"""事件蛰伏量价 — 低位缩量蛰伏后放量突破买入"""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import rolling_high, rolling_low, sma

META = {
    "id": "event_volatility",
    "name": "事件蛰伏量价",
    "description": "低位缩量蛰伏后放量突破买入，止损/止盈/暴跌/持仓到期卖出。",
}

# ============ 可调参数 ============

# -- 买入 --
RANGE_POS_MAX = 0.45          # 股价在60日区间低位上限
LOW_60_RATIO_MIN = 1.08       # 距60日低点至少8%
QUIET_RATIO_MAX = 0.95        # 缩量系数上限
VOL_RATIO_BREAK_MIN = 1.8     # 放量突破量比下限
PRICE_CONFIRM_RATIO = 0.96    # 突破60日高点比例下限
CHANGE_PCT_STRONG_MIN = 4.5   # 强势突破涨幅下限(%)

# -- 卖出 --
STOP_LOSS_PCT = -10           # 硬止损(%)
TAKE_PROFIT_PCT = 28          # 止盈(%)
MAX_HOLD_DAYS = 8             # 最大持仓天数
DAILY_CRASH_PCT = -8          # 单日暴跌离场(%)


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    vol5 = sma(volumes, 5)
    vol20 = sma(volumes, 20)
    high60 = rolling_high([b["high"] for b in bars], 60)
    low60 = rolling_low([b["low"] for b in bars], 60)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None

    for i in range(61, len(bars)):
        if not _ready(ma20, ma60, vol5, vol20, high60, low60, index=i):
            continue

        close = closes[i]
        prev_close = closes[i - 1]
        volume = volumes[i]
        change_pct = (close / prev_close - 1) * 100 if prev_close else 0
        range_pos = _range_position(close, low60[i], high60[i])
        volume_ratio = volume / vol20[i] if vol20[i] else 0
        vol5_ratio = vol5[i] / vol20[i] if vol20[i] else 0
        daily_chg = (close - prev_close) / prev_close if prev_close > 0 else 0

        if not in_pos:
            if _has_limit_down_cluster(closes, i):
                continue

            low_base = range_pos <= RANGE_POS_MAX and close >= low60[i] * LOW_60_RATIO_MIN
            quiet_before = vol5_ratio <= QUIET_RATIO_MAX
            volume_break = volume_ratio >= VOL_RATIO_BREAK_MIN and close > ma20[i] and close > ma60[i]
            price_confirm = close > high60[i - 1] * PRICE_CONFIRM_RATIO or change_pct >= CHANGE_PCT_STRONG_MIN

            if low_base and quiet_before and volume_break and price_confirm:
                in_pos = True
                entry_price = close
                entry_index = i
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": "低位蛰伏后放量启动",
                })
            continue

        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0

        reason = None

        if profit_pct <= STOP_LOSS_PCT:
            reason = f"止损({profit_pct:.1f}%)"
        elif profit_pct >= TAKE_PROFIT_PCT:
            reason = f"止盈({profit_pct:.1f}%)"
        elif daily_chg <= DAILY_CRASH_PCT / 100:
            reason = f"单日暴跌({daily_chg:.1%})"
        elif hold_days >= MAX_HOLD_DAYS:
            reason = f"持仓{hold_days}天到期"

        if reason is None:
            continue

        signals.append({
            "date": bars[i]["trade_date"],
            "action": "sell",
            "reason": reason,
        })
        in_pos = False
        entry_price = None
        entry_index = None

    return signals


def _ready(*series, index):
    return all(item[index] is not None and item[index - 1] is not None for item in series)


def _range_position(close, low, high):
    if high is None or low is None or high <= low:
        return 0.5
    return (close - low) / (high - low)


def _has_limit_down_cluster(closes, index):
    drops = 0
    for j in range(max(1, index - 4), index + 1):
        prev = closes[j - 1]
        if prev and (closes[j] / prev - 1) <= -0.095:
            drops += 1
    return drops >= 2


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
