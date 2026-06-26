# -*- coding: utf-8 -*-
"""吸筹试盘量价 — 中低位缩量整理后温和放量试盘买入"""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import rolling_high, rolling_low, sma

META = {
    "id": "accumulation_probe",
    "name": "吸筹试盘量价",
    "description": "中低位缩量整理后温和放量试盘买入，止损/止盈/暴跌/持仓到期卖出。",
}

# ============ 可调参数 ============

# -- 买入 --
RANGE_POS_MIN = 0.30          # 股价在120日区间低位下限
RANGE_POS_MAX = 0.72          # 股价在120日区间低位上限
CLOSE_TO_120H_MAX = 0.93      # 距120日高点不超过93%
QUIET_RATIO_MAX = 0.95        # 缩量系数上限
VOL_RATIO_BUY_MIN = 1.35      # 当日量比下限
VOL_RATIO_BUY_MAX = 3.0       # 当日量比上限
CHANGE_PCT_MIN = 1.8          # 当日涨幅下限(%)
CHANGE_PCT_MAX = 8.0          # 当日涨幅上限(%)
NOT_HOT_MAX = 0.16            # 近5日累计涨幅上限
AMOUNT_MIN = 150_000_000      # 最低成交额

# -- 卖出 --
STOP_LOSS_PCT = -7.5          # 硬止损(%)
TAKE_PROFIT_PCT = 14          # 止盈(%)
MAX_HOLD_DAYS = 14            # 最大持仓天数
DAILY_CRASH_PCT = -8          # 单日暴跌离场(%)


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    amounts = [b.get("amount") or 0 for b in bars]

    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ma120 = sma(closes, 120)
    vol5 = sma(volumes, 5)
    vol20 = sma(volumes, 20)
    high5 = rolling_high(highs, 5)
    high10 = rolling_high(highs, 10)
    high120 = rolling_high(highs, 120)
    low120 = rolling_low(lows, 120)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None

    for i in range(120, len(bars)):
        if not _ready((ma20, ma60, ma120, vol5, vol20, high5, high10, high120, low120), i):
            continue

        close = closes[i]
        prev_close = closes[i - 1]
        volume = volumes[i]
        amount = amounts[i]
        change_pct = (close / prev_close - 1) * 100 if prev_close else 0
        volume_ratio = volume / vol20[i] if vol20[i] else 0
        quiet_ratio = vol5[i - 2] / vol20[i - 2] if vol20[i - 2] else 99
        range_pos = _range_position(close, low120[i], high120[i])
        daily_chg = (close - prev_close) / prev_close if prev_close > 0 else 0

        trend_ok = close > ma20[i] and close > ma60[i] and ma20[i] >= ma20[i - 1]
        market_ok = ma120[i] >= ma120[i - 1] or close > ma120[i]

        if not in_pos:
            setup_ok = RANGE_POS_MIN <= range_pos <= RANGE_POS_MAX and close <= high120[i] * CLOSE_TO_120H_MAX
            quiet_ok = quiet_ratio <= QUIET_RATIO_MAX
            breakout_ok = close >= high5[i - 1] and close >= high10[i - 1] * 0.985
            volume_ok = VOL_RATIO_BUY_MIN <= volume_ratio <= VOL_RATIO_BUY_MAX
            price_ok = CHANGE_PCT_MIN <= change_pct <= CHANGE_PCT_MAX
            not_hot = close / closes[max(i - 5, 0)] - 1 <= NOT_HOT_MAX
            amount_ok = amount >= AMOUNT_MIN

            if (setup_ok and quiet_ok and breakout_ok and volume_ok
                    and price_ok and trend_ok and market_ok and not_hot and amount_ok):
                in_pos = True
                entry_price = close
                entry_index = i
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": "缩量整理后放量试盘",
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


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
