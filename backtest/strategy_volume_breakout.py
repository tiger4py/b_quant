from backtest.indicators import sma

META = {
    "id": "volume_breakout",
    "name": "放量突破",
    "description": "收盘突破20日均线且成交量大于5日均量1.8倍买入，跌破20日均线卖出。",
}


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    ma20 = sma(closes, 20)
    vol5 = sma(volumes, 5)
    signals = []
    for i in range(1, len(bars)):
        if ma20[i - 1] is None or ma20[i] is None or vol5[i] is None:
            continue
        if closes[i - 1] <= ma20[i - 1] and closes[i] > ma20[i] and volumes[i] > vol5[i] * 1.8:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "放量突破 MA20"})
        elif closes[i - 1] >= ma20[i - 1] and closes[i] < ma20[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "跌破 MA20"})
    return signals
