from backtest.indicators import sma

META = {
    "id": "ma_cross",
    "name": "MA5/MA20 金叉死叉",
    "description": "5日均线上穿20日均线买入，下穿卖出。",
}


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    ma5 = sma(closes, 5)
    ma20 = sma(closes, 20)
    signals = []
    for i in range(1, len(bars)):
        if ma5[i - 1] is None or ma20[i - 1] is None:
            continue
        if ma5[i - 1] <= ma20[i - 1] and ma5[i] > ma20[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "MA5 上穿 MA20"})
        elif ma5[i - 1] >= ma20[i - 1] and ma5[i] < ma20[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "MA5 下穿 MA20"})
    return signals
