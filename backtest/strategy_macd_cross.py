from backtest.indicators import ema

META = {
    "id": "macd_cross",
    "name": "MACD 金叉死叉",
    "description": "DIF 上穿 DEA 买入，下穿卖出。",
}


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    dif = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    dea = ema(dif, 9)
    signals = []
    for i in range(1, len(bars)):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "DIF 上穿 DEA"})
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "DIF 下穿 DEA"})
    return signals
