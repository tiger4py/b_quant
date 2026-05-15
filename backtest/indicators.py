def sma(values, window):
    result = []
    for i in range(len(values)):
        if i + 1 < window:
            result.append(None)
            continue
        chunk = values[i + 1 - window:i + 1]
        result.append(sum(chunk) / window)
    return result


def ema(values, window):
    result = []
    alpha = 2 / (window + 1)
    last = None
    for value in values:
        last = value if last is None else alpha * value + (1 - alpha) * last
        result.append(last)
    return result


def rsi(values, window=14):
    result = [None] * len(values)
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
        if i < window:
            continue
        avg_gain = sum(gains[-window:]) / window
        avg_loss = sum(losses[-window:]) / window
        if avg_loss == 0:
            result[i] = 100
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - 100 / (1 + rs)
    return result


def rolling_high(values, window):
    result = []
    for i in range(len(values)):
        if i + 1 < window:
            result.append(None)
        else:
            result.append(max(values[i + 1 - window:i + 1]))
    return result


def rolling_low(values, window):
    result = []
    for i in range(len(values)):
        if i + 1 < window:
            result.append(None)
        else:
            result.append(min(values[i + 1 - window:i + 1]))
    return result


def stddev(values, window):
    result = []
    for i in range(len(values)):
        if i + 1 < window:
            result.append(None)
            continue
        chunk = values[i + 1 - window:i + 1]
        mean = sum(chunk) / window
        result.append((sum((v - mean) ** 2 for v in chunk) / window) ** 0.5)
    return result


def atr(bars, window=14):
    result = [None] * len(bars)
    trs = []
    for i, bar in enumerate(bars):
        prev_close = bars[i - 1]["close"] if i > 0 else bar["close"]
        tr = max(
            bar["high"] - bar["low"],
            abs(bar["high"] - prev_close),
            abs(bar["low"] - prev_close),
        )
        trs.append(tr)
        if i + 1 >= window:
            result[i] = sum(trs[-window:]) / window
    return result
