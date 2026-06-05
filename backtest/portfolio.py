def run_portfolio_backtest(
    bars_by_code,
    stock_map,
    strategy,
    initial_cash=1000000.0,
    max_positions=5,
):
    max_position_cash = initial_cash / max_positions
    signal_by_date = {}
    all_dates = set()
    bar_lookup = {}
    market_stats = _build_market_stats(bars_by_code)

    for code, bars in bars_by_code.items():
        clean_bars = [b for b in bars if b.get("close") and b.get("open")]
        if len(clean_bars) < 30:
            continue
        bar_lookup[code] = {b["trade_date"]: b for b in clean_bars}
        all_dates.update(bar_lookup[code].keys())
        for signal in strategy.generate_signals(clean_bars):
            signal_by_date.setdefault(signal["date"], []).append({
                "code": code,
                "action": signal["action"],
                "reason": signal.get("reason", ""),
            })

    dates = sorted(all_dates)
    if not dates:
        raise ValueError("没有可用于组合回测的历史数据")

    cash = float(initial_cash)
    positions = {}
    trades = []
    equity_curve = []
    last_price = {}
    gate_history = []

    for date in dates:
        for code, lookup in bar_lookup.items():
            if date in lookup:
                last_price[code] = float(lookup[date]["close"])

        todays_signals = signal_by_date.get(date, [])
        sell_signals = [s for s in todays_signals if s["action"] == "sell"]
        buy_signals = [s for s in todays_signals if s["action"] == "buy"]
        gate = None
        if hasattr(strategy, "market_gate"):
            gate = strategy.market_gate(date, market_stats)
            gate_history.append({"date": date, **gate})
            if not gate["allowed"]:
                buy_signals = []
        elif hasattr(strategy, "allow_buy"):
            allowed = strategy.allow_buy(date, market_stats)
            gate_history.append({
                "date": date,
                "allowed": bool(allowed),
                "reasons": ["市场环境允许进攻"] if allowed else ["市场环境过滤阻止开仓"],
            })
            if not allowed:
                buy_signals = []

        for signal in sell_signals:
            code = signal["code"]
            pos = positions.get(code)
            bar = bar_lookup.get(code, {}).get(date)
            if not pos or not bar:
                continue
            if pos["buy_date"] == date:
                continue
            sell_price = float(bar["close"])
            income = pos["shares"] * sell_price
            cash += income
            cost = pos["shares"] * pos["buy_price"]
            profit = income - cost
            profit_pct = profit / cost * 100 if cost else 0
            stock = stock_map.get(code, {"name": code})
            trades.append({
                "strategy_id": strategy.META["id"],
                "strategy_name": strategy.META["name"],
                "code": code,
                "name": stock["name"],
                "buy_date": pos["buy_date"],
                "buy_price": round(pos["buy_price"], 3),
                "sell_date": date,
                "sell_price": round(sell_price, 3),
                "shares": pos["shares"],
                "buy_amount": round(cost, 2),
                "sell_amount": round(income, 2),
                "profit": round(profit, 2),
                "profit_pct": round(profit_pct, 2),
                "buy_reason": pos["buy_reason"],
                "sell_reason": signal["reason"] or "策略卖出",
            })
            del positions[code]

        buy_candidates = []
        for signal in buy_signals:
            code = signal["code"]
            if code in positions or len(positions) >= max_positions:
                continue
            bar = bar_lookup.get(code, {}).get(date)
            if not bar:
                continue
            stock = stock_map.get(code, {})
            buy_candidates.append((stock.get("latest_amount") or 0, signal, bar))

        buy_candidates.sort(reverse=True, key=lambda x: x[0])
        for _, signal, bar in buy_candidates:
            if len(positions) >= max_positions:
                break
            price = float(bar["close"])
            budget = min(max_position_cash, cash)
            shares = int(budget // price // 100 * 100)
            if shares <= 0:
                continue
            cost = shares * price
            cash -= cost
            positions[signal["code"]] = {
                "buy_date": date,
                "buy_price": price,
                "shares": shares,
                "buy_reason": signal["reason"] or "策略买入",
            }

        equity = cash
        for code, pos in positions.items():
            price = last_price.get(code, pos["buy_price"])
            equity += pos["shares"] * price
        equity_curve.append({
            "date": date,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "position_count": len(positions),
        })

    last_date = dates[-1]
    for code, pos in list(positions.items()):
        sell_price = last_price.get(code, pos["buy_price"])
        income = pos["shares"] * sell_price
        cash += income
        cost = pos["shares"] * pos["buy_price"]
        profit = income - cost
        profit_pct = profit / cost * 100 if cost else 0
        stock = stock_map.get(code, {"name": code})
        trades.append({
            "strategy_id": strategy.META["id"],
            "strategy_name": strategy.META["name"],
            "code": code,
            "name": stock["name"],
            "buy_date": pos["buy_date"],
            "buy_price": round(pos["buy_price"], 3),
            "sell_date": last_date,
            "sell_price": round(sell_price, 3),
            "shares": pos["shares"],
            "buy_amount": round(cost, 2),
            "sell_amount": round(income, 2),
            "profit": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "buy_reason": pos["buy_reason"],
            "sell_reason": "回测结束平仓",
        })
        del positions[code]

    if equity_curve:
        equity_curve[-1]["equity"] = round(cash, 2)
        equity_curve[-1]["cash"] = round(cash, 2)
        equity_curve[-1]["position_count"] = 0

    final_equity = cash
    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] <= 0]
    stock_summaries = _stock_summaries(trades)
    max_drawdown = _max_drawdown([p["equity"] for p in equity_curve])

    return {
        "strategy": strategy.META,
        "summary": {
            "initial_cash": round(initial_cash, 2),
            "final_equity": round(final_equity, 2),
            "total_return_pct": round((final_equity - initial_cash) / initial_cash * 100, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "trade_count": len(trades),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "avg_profit_pct": round(sum(t["profit_pct"] for t in trades) / len(trades), 2) if trades else 0,
            "profit_factor": _profit_factor(wins, losses),
            "max_positions": max_positions,
            "max_position_cash": round(max_position_cash, 2),
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "stock_summaries": stock_summaries,
        "market_gate": _summarize_market_gate(gate_history),
    }


def _stock_summaries(trades):
    grouped = {}
    for trade in trades:
        item = grouped.setdefault(trade["code"], {
            "code": trade["code"],
            "name": trade["name"],
            "profit": 0,
            "trade_count": 0,
            "wins": 0,
        })
        item["profit"] += trade["profit"]
        item["trade_count"] += 1
        if trade["profit"] > 0:
            item["wins"] += 1

    result = []
    for item in grouped.values():
        item["profit"] = round(item["profit"], 2)
        item["win_rate_pct"] = round(item["wins"] / item["trade_count"] * 100, 2) if item["trade_count"] else 0
        result.append(item)
    result.sort(key=lambda x: x["profit"], reverse=True)
    return result


def _build_market_stats(bars_by_code):
    by_date = {}
    for code, bars in bars_by_code.items():
        clean = [b for b in bars if b.get("close") and b.get("open")]
        is_growth = code.startswith("sz.300") or code.startswith("sh.688")
        for i in range(1, len(clean)):
            prev_close = float(clean[i - 1]["close"])
            close = float(clean[i]["close"])
            amount = float(clean[i].get("amount") or 0)
            change_pct = (close / prev_close - 1) * 100 if prev_close else 0
            item = by_date.setdefault(clean[i]["trade_date"], {
                "amount": 0.0,
                "advancers": 0,
                "decliners": 0,
                "flat": 0,
                "limit_up": 0,
                "limit_down": 0,
                "growth_amount": 0.0,
                "growth_advancers": 0,
                "growth_decliners": 0,
            })
            item["amount"] += amount
            if change_pct > 0.2:
                item["advancers"] += 1
                if is_growth:
                    item["growth_advancers"] += 1
            elif change_pct < -0.2:
                item["decliners"] += 1
                if is_growth:
                    item["growth_decliners"] += 1
            else:
                item["flat"] += 1
            if change_pct >= 9.5:
                item["limit_up"] += 1
            elif change_pct <= -9.5:
                item["limit_down"] += 1
            if is_growth:
                item["growth_amount"] += amount

    dates = sorted(by_date)
    trailing_amounts = []
    for date in dates:
        item = by_date[date]
        trailing_amounts.append(item["amount"])
        recent = trailing_amounts[-20:]
        item["amount_ma20"] = sum(recent) / len(recent)
        movers = item["advancers"] + item["decliners"]
        item["breadth"] = item["advancers"] / movers if movers else 0.5
        item["limit_balance"] = item["limit_up"] - item["limit_down"]
        growth_movers = item["growth_advancers"] + item["growth_decliners"]
        item["growth_breadth"] = item["growth_advancers"] / growth_movers if growth_movers else item["breadth"]
        item["growth_amount_share"] = item["growth_amount"] / item["amount"] if item["amount"] else 0
    return by_date


def _summarize_market_gate(gate_history):
    if not gate_history:
        return {
            "allowed_days": 0,
            "blocked_days": 0,
            "allowed_rate_pct": 0,
            "recent": [],
            "blocked_reasons": [],
            "blocked_recent_days": [],
        }

    allowed_days = sum(1 for item in gate_history if item.get("allowed"))
    blocked_days = len(gate_history) - allowed_days
    reason_counter = {}
    blocked_recent_days = []
    for item in gate_history:
        if item.get("allowed"):
            continue
        blocked_recent_days.append({
            "date": item["date"],
            "reasons": item.get("reasons", []),
        })
        for reason in item.get("reasons", []):
            reason_counter[reason] = reason_counter.get(reason, 0) + 1

    blocked_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reason_counter.items(), key=lambda x: (-x[1], x[0]))
    ]
    return {
        "allowed_days": allowed_days,
        "blocked_days": blocked_days,
        "allowed_rate_pct": round(allowed_days / len(gate_history) * 100, 2),
        "recent": gate_history[-15:],
        "blocked_reasons": blocked_reasons,
        "blocked_recent_days": blocked_recent_days[-10:],
    }


def _max_drawdown(equity_values):
    if not equity_values:
        return 0
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
