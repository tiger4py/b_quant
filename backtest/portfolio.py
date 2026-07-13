import inspect


def run_portfolio_backtest(
    bars_by_code,
    stock_map,
    strategy,
    initial_cash=1000000.0,
    max_positions=5,
    enable_position_sizing=False,
    enable_circuit_breaker=False,
    start_date=None,
    end_date=None,
):
    """全市场组合回测。

    参数:
        enable_position_sizing: 启用大盘MA60仓位管理（熊市降仓）
        enable_circuit_breaker: 启用组合熔断（回撤>-25%降仓到1只）
    """
    max_position_cash = initial_cash / max_positions
    signal_by_date = {}
    all_dates = set()
    bar_lookup = {}
    market_stats = _build_market_stats(bars_by_code)

    # 大盘指数（等权均价）用于仓位管理
    market_index = _compute_market_index(bars_by_code)
    market_ma60 = _sma_from_dict(market_index, 60)

    for code, bars in bars_by_code.items():
        clean_bars = [b for b in bars if b.get("close") and b.get("open")]
        if len(clean_bars) < 30:
            continue
        bar_lookup[code] = {b["trade_date"]: b for b in clean_bars}
        all_dates.update(bar_lookup[code].keys())
        gen_params = inspect.signature(strategy.generate_signals).parameters
        gen_args = (clean_bars, market_stats) if len(gen_params) > 1 else (clean_bars,)
        for signal in strategy.generate_signals(*gen_args):
            signal_by_date.setdefault(signal["date"], []).append({
                "code": code,
                "action": signal["action"],
                "reason": signal.get("reason", ""),
            })

    amount_ma20 = _build_amount_ma20(bar_lookup)

    dates = sorted(all_dates)
    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]
    if not dates:
        raise ValueError("没有可用于组合回测的历史数据")

    cash = float(initial_cash)
    positions = {}
    trades = []
    equity_curve = []
    last_price = {}
    gate_history = []
    portfolio_peak = initial_cash  # 组合净值峰值，用于熔断判定

    for date in dates:
        for code, lookup in bar_lookup.items():
            if date in lookup:
                last_price[code] = float(lookup[date]["close"])

        is_last_date = (date == dates[-1])
        todays_signals = signal_by_date.get(date, [])
        sell_signals = [s for s in todays_signals if s["action"] == "sell"]
        buy_signals = [s for s in todays_signals if s["action"] == "buy"]

        # 最后一天照常处理卖出信号（止盈/止损等不因期末而跳过）

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

        # 动态仓位上限：大盘MA60下方降仓 + 组合熔断
        effective_max = max_positions
        if enable_position_sizing or enable_circuit_breaker:
            effective_max = _get_effective_max_positions(
                max_positions, date, market_index, market_ma60,
                portfolio_peak, cash + sum(
                    positions[c]["shares"] * last_price.get(c, positions[c]["buy_price"])
                    for c in positions
                ),
                enable_sizing=enable_position_sizing,
                enable_circuit=enable_circuit_breaker,
            )

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
        # 动态单只仓位上限：总现金 / 剩余可用仓位
        remaining_slots = max(1, effective_max - len(positions))
        dyn_position_cash = cash / remaining_slots
        for signal in buy_signals:
            code = signal["code"]
            if code in positions or len(positions) >= effective_max:
                continue
            bar = bar_lookup.get(code, {}).get(date)
            if not bar:
                continue
            sort_key = amount_ma20.get(code, {}).get(date, bar.get("amount") or 0)
            buy_candidates.append((sort_key, signal, bar))

        buy_candidates.sort(reverse=True, key=lambda x: x[0])
        for _, signal, bar in buy_candidates:
            if len(positions) >= effective_max:
                break
            price = float(bar["close"])
            budget = min(dyn_position_cash, cash)
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
        portfolio_peak = max(portfolio_peak, equity)

    # 期末不平仓，持仓自然保留。未平仓记录写入 trades（标记为 open）
    last_date = dates[-1] if dates else None
    for code, pos in positions.items():
        stock = stock_map.get(code, {"name": code})
        last_px = last_price.get(code, pos["buy_price"])
        cost = pos["shares"] * pos["buy_price"]
        cur_val = pos["shares"] * last_px
        profit_val = cur_val - cost
        pct = (last_px / pos["buy_price"] - 1) * 100 if pos["buy_price"] else 0
        trades.append({
            "strategy_id": strategy.META["id"],
            "strategy_name": strategy.META["name"],
            "code": code,
            "name": stock["name"],
            "buy_date": pos["buy_date"],
            "buy_price": round(pos["buy_price"], 3),
            "sell_date": last_date,
            "sell_price": round(last_px, 3),
            "shares": pos["shares"],
            "buy_amount": round(cost, 2),
            "sell_amount": round(cur_val, 2),
            "profit": round(profit_val, 2),
            "profit_pct": round(pct, 2),
            "buy_reason": pos["buy_reason"],
            "sell_reason": "期末持仓",
        })

    if equity_curve:
        final_equity = equity_curve[-1]["equity"]
    else:
        final_equity = cash
    wins = [t for t in trades if t["profit"] is not None and t["profit"] > 0]
    losses = [t for t in trades if t["profit"] is not None and t["profit"] <= 0]
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
            "win_rate_pct": round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2),
            "avg_profit_pct": round(sum(t["profit_pct"] for t in trades if t["profit_pct"] is not None) / max(1, sum(1 for t in trades if t["profit_pct"] is not None)), 2),
            "profit_factor": _profit_factor(wins, losses),
            "max_positions": max_positions,
            "max_position_cash": round(max_position_cash, 2),
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "stock_summaries": stock_summaries,
        "market_gate": _summarize_market_gate(gate_history),
    }


def _build_amount_ma20(bar_lookup, window=20):
    """预计算每只股票每日的前N日均成交额（无未来函数）。

    返回: {code: {date: ma20_amount}}
    """
    result = {}
    for code, date_bars in bar_lookup.items():
        sorted_dates = sorted(date_bars)
        amounts = [date_bars[d].get("amount") or 0 for d in sorted_dates]
        ma20 = {}
        rolling = []
        for i, (d, amt) in enumerate(zip(sorted_dates, amounts)):
            rolling.append(amt)
            if len(rolling) > window:
                rolling = rolling[-window:]
            if len(rolling) >= window or i >= 5:
                ma20[d] = sum(rolling) / len(rolling)
        if ma20:
            result[code] = ma20
    return result


def _stock_summaries(trades):
    from datetime import datetime as _dt

    grouped = {}
    for trade in trades:
        item = grouped.setdefault(trade["code"], {
            "code": trade["code"],
            "name": trade["name"],
            "profit": 0,
            "trade_count": 0,
            "wins": 0,
            "hold_days_total": 0,
        })
        if trade["profit"] is None:
            continue  # 未平仓，不参与统计
        item["profit"] += trade["profit"]
        item["trade_count"] += 1
        if trade["profit"] > 0:
            item["wins"] += 1
        # 持股天数
        try:
            buy_d = _dt.strptime(trade["buy_date"], "%Y-%m-%d")
            sell_d = _dt.strptime(trade["sell_date"], "%Y-%m-%d")
            item["hold_days_total"] += (sell_d - buy_d).days
        except (ValueError, KeyError):
            pass

    result = []
    for item in grouped.values():
        item["profit"] = round(item["profit"], 2)
        item["win_rate_pct"] = round(item["wins"] / item["trade_count"] * 100, 2) if item["trade_count"] else 0
        item["avg_hold_days"] = round(item["hold_days_total"] / item["trade_count"], 1) if item["trade_count"] else 0
        item.pop("hold_days_total", None)
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


# ============ 组合层优化：仓位管理 + 熔断 ============

def _compute_market_index(bars_by_code):
    """计算等权市场指数（所有股票每日均价）。"""
    by_date = {}
    for code, bars in bars_by_code.items():
        for bar in bars:
            date = bar["trade_date"]
            close = bar.get("close")
            if not close:
                continue
            by_date.setdefault(date, []).append(float(close))

    result = {}
    for date, closes in by_date.items():
        if closes:
            result[date] = sum(closes) / len(closes)
    return result


def _sma_from_dict(value_by_date, window):
    """对按日期索引的 dict 计算 SMA。"""
    dates = sorted(value_by_date)
    result = {}
    for i, date in enumerate(dates):
        if i + 1 < window:
            result[date] = None
            continue
        chunk = [value_by_date[dates[j]] for j in range(i + 1 - window, i + 1)]
        result[date] = sum(chunk) / window
    return result


def _get_effective_max_positions(
    max_positions, date, market_index, market_ma60,
    portfolio_peak, current_equity,
    enable_sizing=True, enable_circuit=True,
):
    """动态仓位上限。

    1. 仓位管理：市场均价 < MA60 → 仓位减半（熊市少做）
    2. 组合熔断：回撤 > 25% → 强制降到 1 仓
    """
    effective = max_positions

    # 1. 大盘 MA60 仓位管理
    if enable_sizing:
        idx_val = market_index.get(date)
        ma60 = market_ma60.get(date)
        if idx_val is not None and ma60 is not None and idx_val < ma60:
            effective = max(1, max_positions // 2)

    # 2. 组合熔断
    if enable_circuit and portfolio_peak > 0:
        dd_pct = (current_equity - portfolio_peak) / portfolio_peak * 100
        if dd_pct <= -25:
            effective = 1

    return effective


# ============ 多策略轮动 ============

def run_multi_strategy_backtest(
    bars_by_code,
    stock_map,
    strategies,           # list of strategy modules, each must have META and generate_signals
    initial_cash=1000000.0,
    max_positions=5,
    enable_position_sizing=True,
    enable_circuit_breaker=True,
    start_date=None,
    end_date=None,
):
    """多策略组合回测。

    精简规则：所有策略信号自由竞争总仓位，按成交额排序择优买入。
    - 涨跌比 < 0.30（恐慌）→ 禁用趋势跟随，只用 V反
    - 涨跌比 > 0.45（强势）→ 趋势跟随和 V反自由竞争
    - 其余中性 → 两者自由竞争
    仓位管理 + 熔断仍然生效。
    """
    if not strategies:
        raise ValueError("至少需要一个策略")

    # 假设 strategies = [trend_following, volatility_breakout]
    TF_IDX = 0
    VR_IDX = 1 if len(strategies) > 1 else -1

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
        for si, strat in enumerate(strategies):
            gen_params = inspect.signature(strat.generate_signals).parameters
            gen_args = (clean_bars, market_stats) if len(gen_params) > 1 else (clean_bars,)
            for signal in strat.generate_signals(*gen_args):
                signal_by_date.setdefault(signal["date"], []).append({
                    "code": code,
                    "action": signal["action"],
                    "reason": signal.get("reason", ""),
                    "strategy_index": si,
                })

    amount_ma20 = _build_amount_ma20(bar_lookup)

    dates = sorted(all_dates)
    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]
    if not dates:
        raise ValueError("没有可用于组合回测的历史数据")

    market_index = _compute_market_index(bars_by_code)
    market_ma60 = _sma_from_dict(market_index, 60)

    cash = float(initial_cash)
    positions = {}
    trades = []
    equity_curve = []
    last_price = {}
    portfolio_peak = initial_cash

    for date in dates:
        for code, lookup in bar_lookup.items():
            if date in lookup:
                last_price[code] = float(lookup[date]["close"])

        is_last_date = (date == dates[-1])
        todays_signals = signal_by_date.get(date, [])
        sell_signals = [s for s in todays_signals if s["action"] == "sell"]
        buy_signals = [s for s in todays_signals if s["action"] == "buy"]

        # 最后一天照常处理卖出信号（止盈/止损等不因期末而跳过）

        # 市场广度
        today_stats = market_stats.get(date, {})
        breadth = today_stats.get("breadth", 0.5)

        # 按策略检查 market_gate + 市场环境过滤
        allowed_buy = []
        for s in buy_signals:
            si = s["strategy_index"]
            strat = strategies[si]

            # 极端恐慌：无 market_gate 的趋势跟随不买（有 gate 的由策略自己判断）
            if si == TF_IDX and not hasattr(strat, "market_gate") and breadth < 0.30:
                continue

            # 策略自己的 market_gate
            if hasattr(strat, "market_gate"):
                gate = strat.market_gate(date, market_stats)
                if not gate["allowed"]:
                    continue
            allowed_buy.append(s)
        buy_signals = allowed_buy

        # 当前权益
        current_equity = cash + sum(
            positions[c]["shares"] * last_price.get(c, positions[c]["buy_price"])
            for c in positions
        )

        # 动态仓位上限
        effective_max = _get_effective_max_positions(
            max_positions, date, market_index, market_ma60,
            portfolio_peak, current_equity,
            enable_sizing=enable_position_sizing,
            enable_circuit=enable_circuit_breaker,
        )

        # 处理卖出
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
            strat = strategies[pos.get("strategy_index", 0)]
            stock = stock_map.get(code, {"name": code})
            trades.append({
                "strategy_id": strat.META["id"],
                "strategy_name": strat.META["name"],
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

        # 处理买入：所有信号自由竞争，按成交额降序择优
        buy_candidates = []
        for signal in buy_signals:
            code = signal["code"]
            if code in positions:
                continue
            bar = bar_lookup.get(code, {}).get(date)
            if not bar:
                continue
            sort_key = amount_ma20.get(code, {}).get(date, bar.get("amount") or 0)
            buy_candidates.append((sort_key, signal, bar))

        buy_candidates.sort(reverse=True, key=lambda x: x[0])
        total_positions = len(positions)
        for _, signal, bar in buy_candidates:
            if total_positions >= effective_max:
                break
            si = signal.get("strategy_index", 0)
            price = float(bar["close"])
            remaining_slots = max(1, effective_max - total_positions)
            dyn_cash = cash / remaining_slots
            budget = min(dyn_cash, cash)
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
                "strategy_index": si,
            }
            total_positions += 1

        # 记录权益
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
        portfolio_peak = max(portfolio_peak, equity)

    # 期末不平仓，未平仓记录写入 trades
    last_date = dates[-1] if dates else None
    for code, pos in positions.items():
        stock = stock_map.get(code, {"name": code})
        strat = strategies[pos.get("strategy_index", 0)]
        last_px = last_price.get(code, pos["buy_price"])
        cost = pos["shares"] * pos["buy_price"]
        cur_val = pos["shares"] * last_px
        profit_val = cur_val - cost
        pct = (last_px / pos["buy_price"] - 1) * 100 if pos["buy_price"] else 0
        trades.append({
            "strategy_id": strat.META["id"],
            "strategy_name": strat.META["name"],
            "code": code,
            "name": stock["name"],
            "buy_date": pos["buy_date"],
            "buy_price": round(pos["buy_price"], 3),
            "sell_date": last_date,
            "sell_price": round(last_px, 3),
            "shares": pos["shares"],
            "buy_amount": round(cost, 2),
            "sell_amount": round(cur_val, 2),
            "profit": round(profit_val, 2),
            "profit_pct": round(pct, 2),
            "buy_reason": pos["buy_reason"],
            "sell_reason": "期末持仓",
        })

    if equity_curve:
        final_equity = equity_curve[-1]["equity"]
    else:
        final_equity = cash
    wins = [t for t in trades if t["profit"] is not None and t["profit"] > 0]
    losses = [t for t in trades if t["profit"] is not None and t["profit"] <= 0]
    max_drawdown = _max_drawdown([p["equity"] for p in equity_curve])

    # 按策略分拆统计
    strategy_breakdown = {}
    for t in trades:
        sid = t["strategy_id"]
        entry = strategy_breakdown.setdefault(sid, {
            "strategy_id": sid,
            "strategy_name": t["strategy_name"],
            "trade_count": 0,
            "wins": 0,
            "profit": 0.0,
        })
        entry["trade_count"] += 1
        entry["profit"] += t["profit"]
        if t["profit"] > 0:
            entry["wins"] += 1

    for v in strategy_breakdown.values():
        v["profit"] = round(v["profit"], 2)
        v["win_rate_pct"] = round(v["wins"] / v["trade_count"] * 100, 2) if v["trade_count"] else 0

    return {
        "strategy": {
            "id": "multi_strategy",
            "name": "多策略轮动",
            "description": "趋势跟随 + 波动率V反，按市场广度动态分配仓位",
        },
        "summary": {
            "initial_cash": round(initial_cash, 2),
            "final_equity": round(final_equity, 2),
            "total_return_pct": round((final_equity - initial_cash) / initial_cash * 100, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "trade_count": len(trades),
            "win_rate_pct": round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2),
            "avg_profit_pct": round(sum(t["profit_pct"] for t in trades if t["profit_pct"] is not None) / max(1, sum(1 for t in trades if t["profit_pct"] is not None)), 2),
            "profit_factor": _profit_factor(wins, losses),
            "max_positions": max_positions,
            "max_position_cash": round(initial_cash / max_positions, 2),
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "stock_summaries": _stock_summaries(trades),
        "strategy_breakdown": sorted(strategy_breakdown.values(), key=lambda x: x["strategy_id"]),
    }
