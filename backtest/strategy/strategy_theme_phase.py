# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import rolling_high, rolling_low, sma

META = {
    "id": "theme_phase",
    "name": "题材阶段量价",
    "description": "低位放量启动或主升回流买入，高位放量滞涨、量增价跌、跌破趋势或久盘不涨卖出。",
}


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
    peak_close = None

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

        if not in_pos:
            if _has_limit_down_cluster(closes, i):
                continue

            base_setup = (
                range_pos <= 0.55
                and close >= low120[i] * 1.10
                and quiet_ratio <= 0.95
                and volume_ratio >= 1.8
                and trend_ok
                and close >= high20[i - 1] * 0.99
                and change_pct >= 2.5
            )

            trend_strength = close >= closes[i - 20] * 1.15
            pullback_days = closes[i - 1] <= high10[i - 1] * 0.96 and vol5[i - 1] <= vol10[i - 1]
            rebreak_setup = (
                trend_strength
                and trend_ok
                and pullback_days
                and volume_ratio >= 1.5
                and close > highs[i - 1]
                and close >= high10[i - 1] * 0.995
            )

            if base_setup or rebreak_setup:
                in_pos = True
                entry_price = close
                entry_index = i
                peak_close = close
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": "低位放量启动" if base_setup else "分歧转强回流",
                })
            continue

        peak_close = max(peak_close, close)
        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0
        drawdown_from_peak = (close / peak_close - 1) * 100 if peak_close else 0
        high_area = range_pos >= 0.78

        high_volume_exit = high_area and volume_ratio >= 2.0 and change_pct <= 1.2
        volume_down_exit = volume_ratio >= 1.4 and change_pct <= -3.0
        trend_break_exit = close < ma20[i] and closes[i - 1] < ma20[i - 1]
        hard_stop_exit = profit_pct <= -8 or drawdown_from_peak <= -12
        event_take_profit = hold_days >= 7 and profit_pct >= 18 and volume_ratio >= 1.6 and change_pct < 2.0
        time_decay_exit = hold_days >= 18 and profit_pct <= 3

        if hard_stop_exit:
            reason = "跌破风控线"
        elif volume_down_exit:
            reason = "量增价跌转弱"
        elif high_volume_exit:
            reason = "高位放量滞涨"
        elif event_take_profit:
            reason = "题材兑现离场"
        elif trend_break_exit:
            reason = "跌破 MA20 趋势"
        elif time_decay_exit:
            reason = "久盘不涨离场"
        else:
            continue

        signals.append({
            "date": bars[i]["trade_date"],
            "action": "sell",
            "reason": reason,
        })
        in_pos = False
        entry_price = None
        entry_index = None
        peak_close = None

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
