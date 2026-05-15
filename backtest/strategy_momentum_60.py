from backtest.indicators import sma

META = {
    "id": "momentum_60",
    "name": "60日动量",
    "description": "60日收益为正且收盘高于20日均线买入，60日收益转负或跌破20日均线卖出。",
}


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    ma20 = sma(closes, 20)
    signals = []
    for i in range(60, len(bars)):
        momentum = closes[i] / closes[i - 60] - 1
        prev_momentum = closes[i - 1] / closes[i - 61] - 1 if i > 60 else 0
        if prev_momentum <= 0 < momentum and closes[i] > ma20[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "60日动量转正且站上 MA20"})
        elif momentum < 0 or closes[i] < ma20[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "动量转弱或跌破 MA20"})
    return signals
