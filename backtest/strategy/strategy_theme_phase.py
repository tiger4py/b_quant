# -*- coding: utf-8 -*-
"""题材阶段量价 — 低位放量启动或主升回流买入"""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import rolling_high, rolling_low, sma

META = {
    "id": "theme_phase",
    "name": "题材阶段量价",
    "description": "低位放量启动或主升回流买入，止损/止盈/暴跌/持仓到期卖出。",
}

# ============ 可调参数 ============

# -- 买入 --
RANGE_POS_MAX = 0.55          # 股价在120日区间低位上限
LOW_120_RATIO_MIN = 1.10      # 距120日低点至少10%
QUIET_RATIO_MAX = 0.95        # 缩量系数上限
VOL_RATIO_BASE_MIN = 1.8      # 低位启动量比下限
CHANGE_PCT_BASE_MIN = 2.5     # 低位启动涨幅下限(%)
TREND_STRENGTH_MIN = 1.15     # 20日涨幅至少15%
PULLBACK_RATIO_MAX = 0.96     # 回踩10日高点比例上限
VOL_RATIO_REBREAK_MIN = 1.5   # 回流量比下限

# -- 卖出 --
STOP_LOSS_PCT = -8            # 硬止损(%)
TAKE_PROFIT_PCT = 18          # 止盈(%)
MAX_HOLD_DAYS = 18            # 最大持仓天数
DAILY_CRASH_PCT = -8          # 单日暴跌离场(%)


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]

    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    vol5 = sma(volumes, 5)
    vol10 = sma(volumes, 10)
    vol20 = sma(volumes, 20)
    high10 = rolling_high(highs, 10)
    high20 = rolling_high(highs, 20)
    high120 = rolling_high(highs, 120)
    low120 = rolling_low(lows, 120)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None

    for i in range(120, len(bars)):
        if not _ready((ma20, ma60, vol5, vol10, vol20, high10, high20, high120, low120), i):
            continue

        close = closes[i]
        prev_close = closes[i - 1]
        volume = volumes[i]
        volume_ratio = volume / vol20[i] if vol20[i] else 0
        quiet_ratio = vol5[i] / vol20[i] if vol20[i] else 0
        trend_ok = close > ma20[i] and close > ma60[i] and ma20[i] >= ma20[i - 1]
        range_pos = _range_position(close, low120[i], high120[i])
        change_pct = (close / prev_close - 1) * 100 if prev_close else 0
        daily_chg = (close - prev_close) / prev_close if prev_close > 0 else 0

        if not in_pos:
            if _has_limit_down_cluster(closes, i):
                continue

            base_setup = (
                range_pos <= RANGE_POS_MAX
                and close >= low120[i] * LOW_120_RATIO_MIN
                and quiet_ratio <= QUIET_RATIO_MAX
                and volume_ratio >= VOL_RATIO_BASE_MIN
                and trend_ok
                and close >= high20[i - 1] * 0.99
                and change_pct >= CHANGE_PCT_BASE_MIN
            )

            trend_strength = close >= closes[i - 20] * TREND_STRENGTH_MIN
            pullback_days = closes[i - 1] <= high10[i - 1] * PULLBACK_RATIO_MAX and vol5[i - 1] <= vol10[i - 1]
            rebreak_setup = (
                trend_strength and trend_ok and pullback_days
                and volume_ratio >= VOL_RATIO_REBREAK_MIN
                and close > highs[i - 1]
                and close >= high10[i - 1] * 0.995
            )

            if base_setup or rebreak_setup:
                in_pos = True
                entry_price = close
                entry_index = i
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": "低位放量启动" if base_setup else "分歧转强回流",
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


def _ready(series_list, index):
    for series in series_list:
        if series[index] is None or series[index - 1] is None:
            return False
    return True


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
