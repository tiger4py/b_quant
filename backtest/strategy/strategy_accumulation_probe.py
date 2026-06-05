# -*- coding: utf-8 -*-
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import rolling_high, rolling_low, sma

META = {
    "id": "accumulation_probe",
    "name": "吸筹试盘量价",
    "description": "中低位缩量整理后温和放量试盘买入，冲高回落、量增价跌、跌破趋势或久盘不涨卖出。",
}


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
    peak_close = None

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

        trend_ok = close > ma20[i] and close > ma60[i] and ma20[i] >= ma20[i - 1]
        market_ok = ma120[i] >= ma120[i - 1] or close > ma120[i]

        if not in_pos:
            setup_ok = 0.30 <= range_pos <= 0.72 and close <= high120[i] * 0.93
            quiet_ok = quiet_ratio <= 0.95
            breakout_ok = close >= high5[i - 1] and close >= high10[i - 1] * 0.985
            volume_ok = 1.35 <= volume_ratio <= 3.0
            price_ok = 1.8 <= change_pct <= 8.0
            not_hot = close / closes[max(i - 5, 0)] - 1 <= 0.16
            amount_ok = amount >= 150_000_000

            if (
                setup_ok
                and quiet_ok
                and breakout_ok
                and volume_ok
                and price_ok
                and trend_ok
                and market_ok
                and not_hot
                and amount_ok
            ):
                in_pos = True
                entry_price = close
                entry_index = i
                peak_close = close
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": "缩量整理后放量试盘",
                })
            continue

        peak_close = max(peak_close, close)
        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0
        drawdown_from_peak = (close / peak_close - 1) * 100 if peak_close else 0

        fake_break_exit = hold_days <= 4 and close < entry_price * 0.972
        high_reversal_exit = hold_days >= 2 and profit_pct >= 6 and change_pct <= -1.8 and drawdown_from_peak <= -4.5
        volume_down_exit = volume_ratio >= 1.4 and change_pct <= -2.5
        trend_break_exit = close < ma20[i] and closes[i - 1] < ma20[i - 1]
        hard_stop_exit = profit_pct <= -7.5 or drawdown_from_peak <= -9
        take_profit_exit = profit_pct >= 14 and (change_pct < 1.8 or volume_ratio >= 2.1)
        time_decay_exit = hold_days >= 14 and profit_pct <= 2

        if fake_break_exit:
            reason = "试盘失败回落"
        elif high_reversal_exit:
            reason = "冲高回落离场"
        elif volume_down_exit:
            reason = "量增价跌转弱"
        elif hard_stop_exit:
            reason = "跌破风控线"
        elif trend_break_exit:
            reason = "跌破 MA20 趋势"
        elif take_profit_exit:
            reason = "修复兑现离场"
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


def allow_buy(date, market_stats):
    return market_gate(date, market_stats)["allowed"]


def market_gate(date, market_stats):
    item = market_stats.get(date)
    if not item:
        return {"allowed": False, "reasons": ["缺少市场数据"]}

    reasons = []
    if item["breadth"] < 0.49:
        reasons.append("市场广度偏弱")
    if item["amount"] < item["amount_ma20"] * 0.95:
        reasons.append("成交额低于20日均值")
    if item["limit_balance"] < -15:
        reasons.append("涨跌停强弱偏空")
    if item["growth_breadth"] < 0.48 and item["growth_amount_share"] < 0.22:
        reasons.append("进攻风格不活跃")

    return {
        "allowed": not reasons,
        "reasons": reasons or ["市场环境允许进攻"],
        "breadth": round(item["breadth"] * 100, 2),
        "amount_yi": round(item["amount"] / 1e8, 2),
        "amount_ma20_yi": round(item["amount_ma20"] / 1e8, 2),
        "limit_balance": item["limit_balance"],
        "growth_breadth": round(item["growth_breadth"] * 100, 2),
        "growth_amount_share": round(item["growth_amount_share"] * 100, 2),
    }


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
