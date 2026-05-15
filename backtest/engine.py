def run_backtest(bars, strategy, initial_cash=100000.0):
    bars = [b for b in bars if b.get("close") and b.get("open")]
    if len(bars) < 30:
        raise ValueError("历史数据太少，至少需要 30 个交易日")

    raw_signals = strategy.generate_signals(bars)
    signal_map = {item["date"]: item for item in raw_signals}

    cash = float(initial_cash)
    shares = 0
    entry = None
    trades = []
    markers = []
    equity_curve = []

    for bar in bars:
        date = bar["trade_date"]
        close = float(bar["close"])
        signal = signal_map.get(date)

        if signal and signal["action"] == "buy" and shares == 0:
            shares = int(cash // close // 100 * 100)
            if shares > 0:
                cost = shares * close
                cash -= cost
                entry = {
                    "date": date,
                    "price": close,
                    "shares": shares,
                    "reason": signal.get("reason", "买入"),
                }
                markers.append({
                    "date": date,
                    "action": "buy",
                    "price": close,
                    "shares": shares,
                    "text": f"买入 {shares}股",
                    "profit_pct": None,
                })

        elif signal and signal["action"] == "sell" and shares > 0 and entry:
            income = shares * close
            cash += income
            profit = income - entry["shares"] * entry["price"]
            profit_pct = profit / (entry["shares"] * entry["price"]) * 100
            trade = {
                "buy_date": entry["date"],
                "buy_price": round(entry["price"], 3),
                "sell_date": date,
                "sell_price": round(close, 3),
                "shares": shares,
                "profit": round(profit, 2),
                "profit_pct": round(profit_pct, 2),
                "buy_reason": entry["reason"],
                "sell_reason": signal.get("reason", "卖出"),
            }
            trades.append(trade)
            markers.append({
                "date": date,
                "action": "sell",
                "price": close,
                "shares": shares,
                "text": f"卖出 {profit_pct:+.2f}%",
                "profit_pct": round(profit_pct, 2),
            })
            shares = 0
            entry = None

        equity = cash + shares * close
        equity_curve.append({
            "date": date,
            "equity": round(equity, 2),
            "close": close,
            "position": shares,
        })

    if shares > 0 and entry:
        last = bars[-1]
        close = float(last["close"])
        income = shares * close
        cash += income
        profit = income - entry["shares"] * entry["price"]
        profit_pct = profit / (entry["shares"] * entry["price"]) * 100
        trades.append({
            "buy_date": entry["date"],
            "buy_price": round(entry["price"], 3),
            "sell_date": last["trade_date"],
            "sell_price": round(close, 3),
            "shares": shares,
            "profit": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "buy_reason": entry["reason"],
            "sell_reason": "回测结束平仓",
        })
        markers.append({
            "date": last["trade_date"],
            "action": "sell",
            "price": close,
            "shares": shares,
            "text": f"平仓 {profit_pct:+.2f}%",
            "profit_pct": round(profit_pct, 2),
        })
        equity_curve[-1]["equity"] = round(cash, 2)
        equity_curve[-1]["position"] = 0

    final_equity = equity_curve[-1]["equity"]
    total_return = (final_equity - initial_cash) / initial_cash * 100
    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] <= 0]
    max_drawdown = _max_drawdown([p["equity"] for p in equity_curve])
    buy_hold_return = (bars[-1]["close"] - bars[0]["close"]) / bars[0]["close"] * 100

    return {
        "strategy": strategy.META,
        "summary": {
            "initial_cash": round(initial_cash, 2),
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(total_return, 2),
            "buy_hold_return_pct": round(buy_hold_return, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "trade_count": len(trades),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "avg_profit_pct": round(sum(t["profit_pct"] for t in trades) / len(trades), 2) if trades else 0,
            "profit_factor": _profit_factor(wins, losses),
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "markers": markers,
    }


def _max_drawdown(equity_values):
    peak = equity_values[0]
    max_dd = 0
    for value in equity_values:
        peak = max(peak, value)
        if peak:
            max_dd = min(max_dd, (value - peak) / peak * 100)
    return abs(max_dd)


def _profit_factor(wins, losses):
    loss_total = abs(sum(t["profit"] for t in losses))
    if loss_total == 0:
        return None if not wins else 999
    return round(sum(t["profit"] for t in wins) / loss_total, 2)
