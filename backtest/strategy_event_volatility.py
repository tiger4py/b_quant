from backtest.indicators import rolling_high, rolling_low, sma

META = {
    "id": "event_volatility",
    "name": "事件蛰伏量价",
    "description": "低位缩量蛰伏后放量突破买入，高位无量持有，高位放量或量增价跌卖出，并规避连续暴跌。",
}


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
    peak_close = None

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

        if not in_pos:
            if _has_limit_down_cluster(closes, i):
                continue

            low_base = range_pos <= 0.45 and close >= low60[i] * 1.08
            quiet_before = vol5_ratio <= 0.95
            volume_break = volume_ratio >= 1.8 and close > ma20[i] and close > ma60[i]
            price_confirm = close > high60[i - 1] * 0.96 or change_pct >= 4.5

            if low_base and quiet_before and volume_break and price_confirm:
                in_pos = True
                entry_price = close
                entry_index = i
                peak_close = close
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": "低位蛰伏后放量启动",
                })
            continue

        peak_close = max(peak_close, close)
        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0
        drawdown_from_peak = (close / peak_close - 1) * 100 if peak_close else 0

        high_area = range_pos >= 0.72
        high_volume_exit = high_area and volume_ratio >= 2.2 and change_pct <= 1.0
        volume_down_exit = volume_ratio >= 1.5 and change_pct <= -3.0
        trend_break_exit = close < ma20[i] and closes[i - 1] < ma20[i - 1]
        hard_stop_exit = profit_pct <= -10 or drawdown_from_peak <= -14
        take_event_money = hold_days >= 8 and profit_pct >= 28 and volume_ratio >= 1.6 and change_pct < 2.5

        if hard_stop_exit:
            reason = "跌破风险线，放弃事件"
        elif volume_down_exit:
            reason = "量增价跌，风险转弱"
        elif high_volume_exit:
            reason = "高位放量，吃波动钱离场"
        elif take_event_money:
            reason = "事件收益兑现，吃了就走"
        elif trend_break_exit:
            reason = "跌破 MA20，蛰伏趋势结束"
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
