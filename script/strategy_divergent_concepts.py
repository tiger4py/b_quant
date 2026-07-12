# -*- coding: utf-8 -*-
"""
股性突变埋伏策略 — 找近期股性转强的概念，提前布局。

核心理念:
  1. 计算每个概念的「股性指纹」(12维特征向量)
  2. 对比近期窗口(10天) vs 历史窗口(60天)的变化
  3. 筛选「偏涨」概念: 收益↑、趋势↑、上涨占比↑、波动放大(有量能)
  4. 等权持有 Top N，定期调仓

用法:
  python script/strategy_divergent_concepts.py
  python script/strategy_divergent_concepts.py --recent 10 --history 60 --top 8 --rebalance 5
"""

from __future__ import annotations
import argparse, csv, glob, json, math, os, sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

DEFAULT_START = "2023-01-01"
DEFAULT_CASH = 1_000_000.0
DEFAULT_TOP = 8
DEFAULT_REBALANCE = 5
DEFAULT_RECENT = 10
DEFAULT_HISTORY = 60
DEFAULT_MAX_HOLD = 30
DEFAULT_MIN_HOLD = 5       # 最少持股天数（新买入不参与下次调仓）
DEFAULT_BUFFER = 5          # 排名缓冲：掉出Top N但在Top N+buffer内保留


# ═══════════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════════

def load_concept_bars():
    csv_dir = str(ROOT_DIR / "data" / "concept")
    grouped = defaultdict(list)
    name_map = {}
    for fp in glob.glob(os.path.join(csv_dir, "*", "*.csv")):
        if not os.path.basename(fp)[:4].isdigit():
            continue
        with open(fp, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row.get("concept_code", "")
                name = row.get("concept_name", "")
                if name: name_map[code] = name
                close = float(row["close"]) if row.get("close") and row["close"] != "None" else None
                if close is None: continue
                vol = row.get("volume"); amt = row.get("amount")
                grouped[code].append({
                    "trade_date": row.get("trade_date", ""),
                    "open": float(row["open"]) if row.get("open") and row["open"] != "None" else close,
                    "high": float(row["high"]) if row.get("high") and row["high"] != "None" else close,
                    "low": float(row["low"]) if row.get("low") and row["low"] != "None" else close,
                    "close": close,
                    "volume": int(float(vol)) if vol and vol != "None" else 0,
                    "amount": float(amt) if amt and amt != "None" else 0,
                })

    bars_by_code = {}
    for code, bars in grouped.items():
        bars.sort(key=lambda b: b["trade_date"])
        if len(bars) >= 150:
            bars_by_code[code] = bars

    return bars_by_code, name_map


# ═══════════════════════════════════════════════════════════════
# 股性特征
# ═══════════════════════════════════════════════════════════════

def compute_features(bars):
    """12维股性特征"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    amounts = [b["amount"] for b in bars if b["amount"] and b["amount"] > 0]
    volumes = [b["volume"] for b in bars]
    n = len(closes)

    # 日收益率
    returns = []
    for i in range(1, n):
        if closes[i-1] > 0:
            returns.append((closes[i] - closes[i-1]) / closes[i-1])
    m = len(returns)
    if m < 5: return None

    return_mean = sum(returns) / m
    return_var = sum((r - return_mean)**2 for r in returns) / m
    return_std = math.sqrt(return_var) if return_var > 0 else 1e-10
    return_skew = sum((r - return_mean)**3 for r in returns) / m / (return_std**3) if return_std > 0 else 0
    up_ratio = sum(1 for r in returns if r > 0) / m

    amount_mean_log = math.log(max(sum(amounts)/len(amounts), 1)) if amounts else 0
    if amounts and len(amounts) > 1:
        avg_a = sum(amounts) / len(amounts)
        amount_cv = math.sqrt(sum((a - avg_a)**2 for a in amounts) / len(amounts)) / avg_a if avg_a > 0 else 0
    else:
        amount_cv = 0

    # 趋势
    xs = list(range(n))
    mx, my = sum(xs)/n, sum(closes)/n
    ss_xy = sum((x-mx)*(y-my) for x,y in zip(xs, closes))
    ss_xx = sum((x-mx)**2 for x in xs)
    ss_yy = sum((y-my)**2 for y in closes)
    trend_slope = (ss_xy / ss_xx) / closes[0] * 100 if ss_xx > 0 and closes[0] > 0 else 0
    trend_r2 = (ss_xy**2) / (ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else 0

    # 最大回撤
    peak = closes[0]; max_dd = 0.0
    for c in closes:
        if c > peak: peak = c
        dd = (peak - c) / peak if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    # 自相关
    if m >= 3:
        cov_ac = sum((returns[i]-return_mean)*(returns[i-1]-return_mean) for i in range(1, m)) / (m-1)
        autocorr_1 = cov_ac / return_var if return_var > 0 else 0
    else:
        autocorr_1 = 0

    # 5日收益
    ret_5d = (closes[-1] - closes[-5]) / closes[-5] if n >= 5 and closes[-5] > 0 else 0

    # 日内振幅
    amps = [(highs[i]-lows[i])/closes[i] for i in range(n) if closes[i] > 0 and 0 < (highs[i]-lows[i])/closes[i] < 0.5]
    amp_mean = sum(amps) / len(amps) if amps else 0

    # 量比 (近5日 vs 前20日)
    if len(volumes) >= 25:
        v5 = sum(volumes[-5:]) / 5
        v20 = sum(volumes[-25:-5]) / 20
        vol_ratio = v5 / v20 if v20 > 0 else 1
    else:
        vol_ratio = 1

    return {
        "return_mean": return_mean, "return_std": return_std,
        "return_skew": return_skew, "up_day_ratio": up_ratio,
        "amount_mean_log": amount_mean_log, "amount_cv": amount_cv,
        "trend_slope_pct": trend_slope, "trend_r2": trend_r2,
        "max_drawdown": max_dd, "autocorr_1": autocorr_1,
        "ret_5d": ret_5d, "amp_mean": amp_mean,
        "vol_ratio": vol_ratio, "n_days": n,
    }


# ═══════════════════════════════════════════════════════════════
# 偏涨评分
# ═══════════════════════════════════════════════════════════════

def bullish_divergence_score(ft_recent, ft_history):
    """计算「偏涨」综合评分。正值 = 股性转好。"""
    score = 0.0

    # 收益转向（权重最高）
    if ft_history.get("return_mean", 0) is not None:
        score += (ft_recent["return_mean"] - ft_history["return_mean"]) * 200
    # 趋势转向
    score += (ft_recent["trend_slope_pct"] - ft_history["trend_slope_pct"]) * 30
    # 上涨占比增加
    score += (ft_recent["up_day_ratio"] - ft_history["up_day_ratio"]) * 20
    # 波动放大（有量能才有行情）
    if ft_history.get("return_std", 0) and ft_history["return_std"] > 0:
        vol_expansion = ft_recent["return_std"] / ft_history["return_std"]
        if 1.2 < vol_expansion < 5:
            score += vol_expansion * 5
    # 回撤减小
    score += (ft_history["max_drawdown"] - ft_recent["max_drawdown"]) * 15
    # 5日动量
    score += ft_recent["ret_5d"] * 10
    # 量比放大
    score += (ft_recent.get("vol_ratio", 1) - 1) * 5
    # 自相关转正（趋势形成）
    score += (ft_recent["autocorr_1"] - ft_history["autocorr_1"]) * 8
    # 偏度转正（大涨倾向）
    score += (ft_recent["return_skew"] - ft_history["return_skew"]) * 3

    return score


# ═══════════════════════════════════════════════════════════════
# 回测
# ═══════════════════════════════════════════════════════════════

def run_backtest(
    bars_by_code, name_map,
    recent_days=10, history_days=60,
    top_n=8, rebalance_days=5, max_hold_days=30,
    min_hold_days=5, buffer_size=5,
    initial_cash=DEFAULT_CASH, start_date=None,
    breadth_min=0.40,  # 市场广度门控
):
    all_dates = sorted(set(
        d for bars in bars_by_code.values()
        for b in bars for d in [b["trade_date"]]
    ))
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]

    cash = float(initial_cash)
    positions = {}  # code -> {buy_date, buy_price, shares, hold_days}
    trades = []
    equity_curve = []
    rebalance_counter = 0

    for di, date in enumerate(all_dates):
        # ---- 权益曲线 ----
        equity = cash
        for code, pos in positions.items():
            # find today's close
            cbars = bars_by_code.get(code, [])
            day_bar = next((b for b in cbars if b["trade_date"] == date), None)
            price = day_bar["close"] if day_bar else pos["buy_price"]
            equity += pos["shares"] * price
        equity_curve.append({"date": date, "equity": round(equity, 2)})

        # ---- 调仓 ----
        if rebalance_counter % rebalance_days != 0:
            rebalance_counter += 1
            for pos in positions.values():
                pos["hold_days"] += 1
            continue

        # ---- 市场广度 ----
        up_count, total_count = 0, 0
        for code, bars in bars_by_code.items():
            day_bars = [b for b in bars if b["trade_date"] == date]
            prev_bars = [b for b in bars if b["trade_date"] < date]
            if day_bars and prev_bars:
                total_count += 1
                if day_bars[0]["close"] > prev_bars[-1]["close"]:
                    up_count += 1
        breadth = up_count / total_count if total_count > 0 else 0.5

        # ---- 计算当日所有概念的偏涨分数 ----
        scores = []
        for code, bars in bars_by_code.items():
            bars_before = [b for b in bars if b["trade_date"] <= date]
            if len(bars_before) < history_days + recent_days:
                continue
            recent_bars = bars_before[-recent_days:]
            hist_bars = bars_before[-recent_days - history_days:-recent_days]
            if len(recent_bars) < recent_days or len(hist_bars) < history_days:
                continue
            ft_r = compute_features(recent_bars)
            ft_h = compute_features(hist_bars)
            if ft_r is None or ft_h is None:
                continue
            score = bullish_divergence_score(ft_r, ft_h)
            if ft_r["trend_slope_pct"] <= 0 and ft_r["ret_5d"] <= 0:
                if ft_r["max_drawdown"] > ft_h["max_drawdown"]:
                    continue
            cur_price = recent_bars[-1]["close"]
            scores.append((score, code, cur_price, ft_r))

        scores.sort(key=lambda x: -x[0])

        # 门控：广度差时空仓/轻仓
        if breadth >= breadth_min:
            effective_top = top_n
            target_codes = {code for _, code, _, _ in scores[:top_n]}
            # 缓冲：掉出 Top N 但在 Top N+buffer 内 → 保留
            buffered_codes = {code for _, code, _, _ in scores[:top_n + buffer_size]}
        else:
            effective_top = min(2, top_n)
            target_codes = {code for _, code, _, _ in scores[:effective_top]}
            buffered_codes = target_codes  # 防守模式无缓冲

        # ---- 卖出 ----
        for code in list(positions):
            pos = positions[code]
            cbars = bars_by_code.get(code, [])
            day_bar = next((b for b in cbars if b["trade_date"] == date), None)
            if not day_bar:
                continue

            sell_price = day_bar["close"]
            sell_reason = None
            hold_days = pos["hold_days"] + rebalance_days

            # 最少持股：刚买不到 min_hold_days 的跳过卖出判断
            if hold_days < min_hold_days:
                pos["hold_days"] = hold_days
                continue

            if code not in buffered_codes:
                sell_reason = f"rank_out"
            elif code not in target_codes:
                # 在缓冲区内但不在Top N → 保留不清仓（sell_reason保持None）
                pos["hold_days"] = hold_days
                continue
            elif hold_days >= max_hold_days:
                sell_reason = f"max_hold"

            if sell_reason is None:
                pos["hold_days"] = hold_days
                continue

            income = pos["shares"] * sell_price
            cost = pos["shares"] * pos["buy_price"]
            cash += income
            trades.append({
                "code": code, "name": name_map.get(code, code),
                "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 3),
                "sell_date": date, "sell_price": round(sell_price, 3),
                "shares": pos["shares"],
                "profit": round(income - cost, 2),
                "profit_pct": round((sell_price / pos["buy_price"] - 1) * 100, 2),
                "buy_reason": pos["buy_reason"],
                "sell_reason": sell_reason,
            })
            del positions[code]

        # ---- 买入（仅广度OK时） ----
        if breadth >= breadth_min:
            for score, code, price, ft in scores[:top_n]:
                if code in positions or len(positions) >= effective_top:
                    continue
                slots_left = max(1, effective_top - len(positions))
                budget = cash / slots_left
                shares = int(budget // price)
                if shares <= 0:
                    continue
                cost = shares * price
                cash -= cost
                positions[code] = {
                    "buy_date": date, "buy_price": price,
                    "shares": shares, "hold_days": 0,
                    "buy_reason": f"score={score:.2f} trend={ft['trend_slope_pct']:+.3f}%/d",
                }

        rebalance_counter += 1

    # ---- 期末平仓 ----
    last_date = all_dates[-1] if all_dates else None
    for code, pos in positions.items():
        cbars = bars_by_code.get(code, [])
        last_bar = next((b for b in reversed(cbars) if b["trade_date"] <= last_date), None)
        sell_price = last_bar["close"] if last_bar else pos["buy_price"]
        income = pos["shares"] * sell_price
        cost = pos["shares"] * pos["buy_price"]
        trades.append({
            "code": code, "name": name_map.get(code, code),
            "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 3),
            "sell_date": last_date, "sell_price": round(sell_price, 3),
            "shares": pos["shares"],
            "profit": round(income - cost, 2),
            "profit_pct": round((sell_price / pos["buy_price"] - 1) * 100, 2),
            "buy_reason": pos["buy_reason"],
            "sell_reason": "期末持仓",
        })

    # ---- 统计 ----
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
    closed = [t for t in trades if t["sell_reason"] != "期末持仓"]
    wins = [t for t in closed if t["profit"] > 0]

    def _max_drawdown(equities):
        peak = equities[0]; max_dd = 0.0
        for e in equities:
            if e > peak: peak = e
            dd = (peak - e) / peak if peak > 0 else 0
            if dd > max_dd: max_dd = dd
        return max_dd * 100

    def _profit_factor(closed_trades):
        gross_profit = sum(t["profit"] for t in closed_trades if t["profit"] > 0)
        gross_loss = abs(sum(t["profit"] for t in closed_trades if t["profit"] < 0))
        return round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    eqs = [x["equity"] for x in equity_curve]

    summary = {
        "strategy": "股性突变埋伏",
        "params": f"recent={recent_days}d history={history_days}d top={top_n} rebal={rebalance_days}d",
        "initial_cash": initial_cash,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity - initial_cash) / initial_cash * 100, 2),
        "max_drawdown_pct": round(_max_drawdown(eqs), 2),
        "trade_count": len(closed),
        "win_rate_pct": round(len(wins) / max(1, len(closed)) * 100, 2),
        "avg_profit_pct": round(sum(t["profit_pct"] for t in closed) / max(1, len(closed)), 2),
        "profit_factor": _profit_factor(closed),
        "open_positions": len(positions),
        "date_range": f"{all_dates[0]} ~ {all_dates[-1]}",
    }

    return {
        "summary": summary,
        "equity_curve": equity_curve,
        "trades": trades,
        "current_positions": [
            {
                "code": code, "name": name_map.get(code, code),
                "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 3),
                "cur_price": round(
                    next((b["close"] for b in reversed(bars_by_code.get(code, []))
                          if b["trade_date"] <= last_date), pos["buy_price"]), 3),
                "shares": pos["shares"],
            }
            for code, pos in positions.items()
        ],
    }


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="股性突变埋伏策略")
    parser.add_argument("--recent", type=int, default=10, help="近期窗口天数")
    parser.add_argument("--history", type=int, default=60, help="历史窗口天数")
    parser.add_argument("--top", type=int, default=8, help="持有概念数")
    parser.add_argument("--rebalance", type=int, default=5, help="调仓周期")
    parser.add_argument("--max-hold", type=int, default=30, help="最大持有天数")
    parser.add_argument("--min-hold", type=int, default=5, help="最少持有天数")
    parser.add_argument("--buffer", type=int, default=5, help="排名缓冲(掉出Top N但在Top N+buffer保留)")
    parser.add_argument("--breadth-min", type=float, default=0.40, help="市场广度门控阈值")
    parser.add_argument("--start", default=DEFAULT_START, help="起始日期")
    parser.add_argument("--cash", type=float, default=DEFAULT_CASH)
    parser.add_argument("--save", action="store_true", default=True)
    args = parser.parse_args()

    print("加载概念数据...")
    bars_by_code, name_map = load_concept_bars()
    print(f"概念池: {len(bars_by_code)} 个")

    result = run_backtest(
        bars_by_code, name_map,
        recent_days=args.recent,
        history_days=args.history,
        top_n=args.top,
        rebalance_days=args.rebalance,
        max_hold_days=args.max_hold,
        min_hold_days=args.min_hold,
        buffer_size=args.buffer,
        initial_cash=args.cash,
        start_date=args.start,
        breadth_min=args.breadth_min,
    )

    s = result["summary"]
    print()
    print("=" * 70)
    print(f"  股性突变埋伏策略 — {s['params']}")
    print(f"  区间: {s['date_range']}")
    print("=" * 70)
    print(f"  收益: {s['total_return_pct']:+.2f}% | 回撤: {s['max_drawdown_pct']:.2f}% | 胜率: {s['win_rate_pct']:.1f}% | PF: {s['profit_factor']}")
    print(f"  交易: {s['trade_count']}笔 | 期末权益: {s['final_equity']:,.0f}")

    # 当前持仓
    positions = result.get("current_positions", [])
    if positions:
        print(f"\n  📊 当前持仓 ({len(positions)}个):")
        for p in positions:
            pnl = (p["cur_price"] / p["buy_price"] - 1) * 100
            print(f"    {p['name']:<16} ({p['code']})  买{p['buy_date']} @{p['buy_price']:.1f}  现{p['cur_price']:.1f}  {pnl:+.1f}%")

    # 最近交易
    closed = [t for t in result["trades"] if t.get("sell_reason") != "期末持仓"]
    if closed:
        print(f"\n  📋 最近10笔:")
        for t in closed[-10:]:
            print(f"    {t['name']:<16} {t['buy_date']} → {t['sell_date']}  {t['profit_pct']:+.1f}%  {t['sell_reason']}")

    # 保存
    if args.save:
        out_dir = ROOT_DIR / "data" / "strategy" / "divergent_concepts"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"{stamp}.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n  已保存: {out_path}")


if __name__ == "__main__":
    main()
