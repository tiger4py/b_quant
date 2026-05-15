from backtest.indicators import rsi

META = {
    "id": "rsi_reversal",
    "name": "RSI 超卖反弹",
    "description": "RSI14 从30下方回升买入，升至70上方卖出。",
}


def generate_signals(bars):
    closes = [b["close"] for b in bars]
    rsi14 = rsi(closes, 14)
    signals = []
    for i in range(1, len(bars)):
        if rsi14[i - 1] is None or rsi14[i] is None:
            continue
        if rsi14[i - 1] < 30 <= rsi14[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "buy", "reason": "RSI 从超卖区回升"})
        elif rsi14[i - 1] < 70 <= rsi14[i]:
            signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": "RSI 进入超买区"})
    return signals
