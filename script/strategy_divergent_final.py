# -*- coding: utf-8 -*-
"""
股性突变埋伏策略 (Divergent Concept Ambush Strategy) — v1 最终版

================================================================================
策略概述
================================================================================

核心理念：找到「股性」正在发生突变的概念板块，在变化初期提前埋伏。

「股性」定义：每个概念板块的 12 维行为特征向量，包括：
  收益率均值、波动率、偏度、上涨占比、成交额、量能变异、
  趋势斜率、趋势R²、最大回撤、自相关、短期动量、日内振幅

策略流程（每 3 天调仓一次）：

  1. 大盘评估 → 判断市场状态 (normal / fast / slow)
  2. 对每个概念，计算「近期 10 天」vs「历史 60 天」的股性变化
  3. 偏涨评分 = 收益转向×200 + 趋势转向×30 + 上涨占比×20 + 波动放大×5 ...
  4. 买偏涨分最高的 Top N 概念，等权分配
  5. 卖出条件：掉出 Top(N+buffer) / 持有超 30 天
  6. 最少持 3 天 + 排名缓冲 5 名 → 减少噪音交易

================================================================================
市场状态自适应
================================================================================

  大盘波动率 > 2.5% 且广度标准差 > 0.12 → fast (13%)
    窗口 7d/40d, 调仓 2d, 持 8 个, 缓冲 4
    含义：高波快轮动 → 缩短窗口更快响应

  大盘波动率 < 1.8% 且广度标准差 < 0.08 → slow (0%)
    空仓
    含义：低波死水 → 信号不可靠，不做

  其余 → normal (87%)
    窗口 10d/60d, 调仓 3d, 持 10 个, 缓冲 5
    含义：主力模式

================================================================================
回测表现 (2023-01-03 ~ 2026-07-10)

  收益: +183.98%    胜率: 53.7%
  最大回撤: 14.50%  盈亏比: 1.69
  交易: 1,158 笔     月度胜率: 70%

  逐年: 2023 +236% | 2024 +425% | 2025 +368% | 2026 +71%

  持股天数分布:
    1-3天  58% (快速止损止盈 → 控制回撤)
    7-15天 34% (主要盈利区间, 胜率 60-74%)
    16-30天 3% (大赢家, 胜率 96-100%)

================================================================================
用法:
  python script/strategy_divergent_final.py                 # 完整回测
  python script/strategy_divergent_final.py --show-holdings  # 只看当前持仓
  python script/strategy_divergent_final.py --start 2024-01-01  # 指定起始日
================================================================================
"""

from __future__ import annotations
import argparse, glob, json, math, os, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from script.strategy_divergent_concepts import (
    load_concept_bars, compute_features, bullish_divergence_score,
)

# ═══════════════════════════════════════════════════════════════
# 参数
# ═══════════════════════════════════════════════════════════════

# normal 状态（主力模式）
NORMAL_PARAMS = {
    "recent": 10, "history": 60, "rebalance": 3,
    "top": 10, "buffer": 5, "min_hold": 3, "breadth_min": 0.40,
}

# fast 状态（高波快轮动）
FAST_PARAMS = {
    "recent": 7, "history": 40, "rebalance": 2,
    "top": 8, "buffer": 4, "min_hold": 3, "breadth_min": 0.40,
}

# slow 状态（空仓）
SLOW_PARAMS = {
    "recent": 14, "history": 80, "rebalance": 5,
    "top": 0, "buffer": 0, "min_hold": 3, "breadth_min": 0.40,
}

MAX_HOLD_DAYS = 30
REGIME_EVAL_DAYS = 10  # 每 10 天评估一次大盘状态


# ═══════════════════════════════════════════════════════════════
# 大盘评估
# ═══════════════════════════════════════════════════════════════

def assess_regime(bars_by_code, date):
    """评估当前大盘状态。"""
    # 1. 市场波动率（近 60 天所有概念收益率标准差）
    all_returns = []
    for code, bars in bars_by_code.items():
        bars_before = [b for b in bars if b["trade_date"] <= date]
        if len(bars_before) < 80: continue
        closes = [b["close"] for b in bars_before[-60:]]
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                all_returns.append((closes[i] - closes[i - 1]) / closes[i - 1])

    if not all_returns:
        return "normal", NORMAL_PARAMS

    mean_ret = sum(all_returns) / len(all_returns)
    market_vol = math.sqrt(sum((r - mean_ret) ** 2 for r in all_returns) / len(all_returns))

    # 2. 广度标准差（近 20 天涨跌比的标准差）
    all_dates = sorted(set(d for bars in bars_by_code.values()
                           for b in bars for d in [b["trade_date"]] if d <= date))
    recent_20 = all_dates[-20:]
    breadth_vals = []
    for d in recent_20:
        up, total = 0, 0
        for code, bars in bars_by_code.items():
            db = next((b for b in bars if b["trade_date"] == d), None)
            pb = next((b for b in bars if b["trade_date"] < d), None)
            if db and pb:
                total += 1
                if db["close"] > pb["close"]: up += 1
        if total > 0:
            breadth_vals.append(up / total)
    breadth_std = math.sqrt(sum((b - sum(breadth_vals) / len(breadth_vals)) ** 2
                                for b in breadth_vals) / len(breadth_vals)) if breadth_vals else 0.1

    # 判定
    if market_vol > 0.025 and breadth_std > 0.12:
        return "fast", FAST_PARAMS
    elif market_vol < 0.018 and breadth_std < 0.08:
        return "slow", SLOW_PARAMS
    else:
        return "normal", NORMAL_PARAMS


def get_market_breadth(bars_by_code, date):
    """当日概念上涨比例。"""
    up, total = 0, 0
    for code, bars in bars_by_code.items():
        db = next((b for b in bars if b["trade_date"] == date), None)
        pb = next((b for b in bars if b["trade_date"] < date), None)
        if db and pb:
            total += 1
            if db["close"] > pb["close"]: up += 1
    return up / total if total > 0 else 0.5


# ═══════════════════════════════════════════════════════════════
# 核心回测
# ═══════════════════════════════════════════════════════════════

def run_strategy(bars_by_code, name_map, initial_cash=1_000_000, start_date="2023-01-01"):
    """运行股性突变埋伏策略。

    返回:
        summary: 统计摘要
        equity_curve: 每日权益
        trades: 所有交易记录
        positions: 期末持仓
        regime_log: 大盘状态变化记录
    """
    all_dates = sorted(set(d for bars in bars_by_code.values()
                           for b in bars for d in [b["trade_date"]]))
    all_dates = [d for d in all_dates if d >= start_date]

    cash = float(initial_cash)
    positions = {}       # code -> {buy_date, buy_price, shares, hold_days}
    trades = []
    equity_curve = []
    regime_log = []

    rebalance_counter = 0
    regime_eval_counter = 0
    cur_regime = "normal"
    cur_params = dict(NORMAL_PARAMS)

    for di, date in enumerate(all_dates):
        # ---- 权益 ----
        equity = cash
        for code, pos in positions.items():
            cbars = bars_by_code.get(code, [])
            db = next((b for b in cbars if b["trade_date"] == date), None)
            price = db["close"] if db else pos["buy_price"]
            equity += pos["shares"] * price
        equity_curve.append({"date": date, "equity": round(equity, 2)})

        # ---- 评估大盘 ----
        if regime_eval_counter % REGIME_EVAL_DAYS == 0:
            cur_regime, cur_params = assess_regime(bars_by_code, date)
            regime_log.append({"date": date, "regime": cur_regime})

        breadth = get_market_breadth(bars_by_code, date)

        # ---- 调仓判断 ----
        rebalance_days = cur_params["rebalance"]
        if rebalance_counter % rebalance_days != 0:
            rebalance_counter += 1
            regime_eval_counter += 1
            for pos in positions.values():
                pos["hold_days"] += 1
            continue

        # ---- 计算所有概念偏涨分 ----
        recent = cur_params["recent"]
        history = cur_params["history"]
        top_n = cur_params["top"]
        buffer_size = cur_params["buffer"]
        breadth_min = cur_params["breadth_min"]
        min_hold = cur_params["min_hold"]

        scores = []
        for code, bars in bars_by_code.items():
            bars_before = [b for b in bars if b["trade_date"] <= date]
            if len(bars_before) < history + recent: continue
            recent_bars = bars_before[-recent:]
            hist_bars = bars_before[-recent - history:-recent]
            if len(recent_bars) < recent or len(hist_bars) < history: continue
            ft_r = compute_features(recent_bars)
            ft_h = compute_features(hist_bars)
            if ft_r is None or ft_h is None: continue
            score = bullish_divergence_score(ft_r, ft_h)
            # 过滤：趋势和收益都负 + 回撤扩大的不买
            if ft_r["trend_slope_pct"] <= 0 and ft_r["ret_5d"] <= 0:
                if ft_r["max_drawdown"] > ft_h["max_drawdown"]:
                    continue
            scores.append((score, code, recent_bars[-1]["close"], ft_r))
        scores.sort(key=lambda x: -x[0])

        # ---- 门控 ----
        if top_n == 0:
            effective_top = 0
            target_codes = set()
            buffered_codes = set()
        elif breadth >= breadth_min:
            effective_top = top_n
            target_codes = {code for _, code, _, _ in scores[:top_n]}
            buffered_codes = {code for _, code, _, _ in scores[:top_n + buffer_size]}
        else:
            effective_top = min(2, top_n)
            target_codes = {code for _, code, _, _ in scores[:effective_top]}
            buffered_codes = target_codes

        # ---- 卖出 ----
        for code in list(positions):
            pos = positions[code]
            cbars = bars_by_code.get(code, [])
            db = next((b for b in cbars if b["trade_date"] == date), None)
            if not db: continue

            sell_price = db["close"]
            hold_days = pos["hold_days"] + rebalance_days

            # 最少持有限制
            if hold_days < min_hold:
                pos["hold_days"] = hold_days
                continue

            # 卖出判断
            if code not in buffered_codes:
                sell_reason = "rank_out"
            elif code not in target_codes:
                # 在缓冲区内 → 保留
                pos["hold_days"] = hold_days
                continue
            elif hold_days >= MAX_HOLD_DAYS:
                sell_reason = "max_hold"
            else:
                pos["hold_days"] = hold_days
                continue

            # 执行卖出
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
                "sell_reason": sell_reason,
                "hold_days": hold_days - rebalance_days,
                "regime": cur_regime,
            })
            del positions[code]

        # ---- 买入 ----
        if breadth >= breadth_min and top_n > 0:
            for score, code, price, ft in scores[:top_n]:
                if code in positions or len(positions) >= effective_top: continue
                slots = max(1, effective_top - len(positions))
                budget = cash / slots
                shares = int(budget // price)
                if shares <= 0: continue
                cash -= shares * price
                positions[code] = {
                    "buy_date": date, "buy_price": price,
                    "shares": shares, "hold_days": 0,
                    "buy_reason": f"score={score:.2f}",
                }

        rebalance_counter += 1
        regime_eval_counter += 1

    # ---- 期末平仓 ----
    last_date = all_dates[-1]
    for code, pos in positions.items():
        cbars = bars_by_code.get(code, [])
        lb = next((b for b in reversed(cbars) if b["trade_date"] <= last_date), None)
        sp = lb["close"] if lb else pos["buy_price"]
        trades.append({
            "code": code, "name": name_map.get(code, code),
            "buy_date": pos["buy_date"], "buy_price": pos["buy_price"],
            "sell_date": last_date, "sell_price": sp,
            "shares": pos["shares"], "profit": round(pos["shares"] * (sp - pos["buy_price"]), 2),
            "profit_pct": round((sp / pos["buy_price"] - 1) * 100, 2),
            "sell_reason": "期末", "regime": cur_regime,
        })

    # ---- 统计 ----
    final_eq = equity_curve[-1]["equity"]
    closed = [t for t in trades if t["sell_reason"] != "期末"]
    wins = [t for t in closed if t["profit"] > 0]
    eqs = [x["equity"] for x in equity_curve]

    peak = eqs[0]; max_dd = 0.0
    for e in eqs:
        if e > peak: peak = e
        dd = (peak - e) / peak if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    gross_p = sum(t["profit"] for t in closed if t["profit"] > 0)
    gross_l = abs(sum(t["profit"] for t in closed if t["profit"] < 0))
    pf = round(gross_p / gross_l, 2) if gross_l > 0 else 99

    summary = {
        "strategy": "股性突变埋伏 v1",
        "date_range": f"{all_dates[0]} ~ {all_dates[-1]}",
        "initial_cash": initial_cash,
        "final_equity": round(final_eq, 2),
        "total_return_pct": round((final_eq - initial_cash) / initial_cash * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trade_count": len(closed),
        "win_rate_pct": round(len(wins) / max(1, len(closed)) * 100, 2),
        "avg_profit_pct": round(sum(t["profit_pct"] for t in closed) / max(1, len(closed)), 2),
        "profit_factor": pf,
    }

    # Regime 统计
    regime_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl_sum": 0.0})
    for t in closed:
        reg = t.get("regime", "?")
        regime_stats[reg]["trades"] += 1
        if t["profit"] > 0: regime_stats[reg]["wins"] += 1
        regime_stats[reg]["pnl_sum"] += t["profit_pct"]

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
        "regime_log": regime_log,
        "regime_stats": {
            reg: {
                "trades": s["trades"],
                "win_rate": round(s["wins"] / max(1, s["trades"]) * 100, 1),
                "avg_pnl": round(s["pnl_sum"] / max(1, s["trades"]), 2),
            }
            for reg, s in regime_stats.items()
        },
    }


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="股性突变埋伏策略 v1")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--cash", type=float, default=1_000_000)
    parser.add_argument("--show-holdings", action="store_true", help="只显示当前持仓")
    args = parser.parse_args()

    print("=" * 70)
    print("  股性突变埋伏策略 v1")
    print("  Divergent Concept Ambush Strategy")
    print("=" * 70)
    print()

    print("加载概念数据...")
    bars_by_code, name_map = load_concept_bars()
    print(f"概念池: {len(bars_by_code)} 个")

    if args.show_holdings:
        # 快速模式：只显示当前排名
        all_dates = sorted(set(d for bars in bars_by_code.values()
                               for b in bars for d in [b["trade_date"]]))
        latest = all_dates[-1]
        _, params = assess_regime(bars_by_code, latest)
        breadth = get_market_breadth(bars_by_code, latest)

        scores = []
        for code, bars in bars_by_code.items():
            bars_before = [b for b in bars if b["trade_date"] <= latest]
            if len(bars_before) < 70: continue
            ft_r = compute_features(bars_before[-10:])
            ft_h = compute_features(bars_before[-70:-10])
            if ft_r is None or ft_h is None: continue
            score = bullish_divergence_score(ft_r, ft_h)
            if ft_r["trend_slope_pct"] <= 0 and ft_r["ret_5d"] <= 0:
                if ft_r["max_drawdown"] > ft_h["max_drawdown"]: continue
            scores.append((score, code, bars_before[-1]["close"], ft_r))
        scores.sort(key=lambda x: -x[0])

        print(f"\n大盤狀態: {_,} | 廣度: {breadth:.3f}")
        print(f"參數: {params}")
        print(f"\n📊 偏漲分 Top {params['top']}:")
        for i, (score, code, price, ft) in enumerate(scores[:params['top']], 1):
            arrow = "↗" if ft['trend_slope_pct'] > 0 else "↘"
            print(f"  {i:>2}. {name_map.get(code, code):<20} "
                  f"score={score:+.2f}  trend={ft['trend_slope_pct']:+.3f}%/d {arrow}  "
                  f"ret5d={ft['ret_5d']:+.2f}%")
        return

    # 完整回测
    print("运行回测...")
    result = run_strategy(bars_by_code, name_map,
                          initial_cash=args.cash, start_date=args.start)

    s = result["summary"]
    print()
    print(f"区间: {s['date_range']}")
    print(f"收益: {s['total_return_pct']:+.2f}% | 回撤: {s['max_drawdown_pct']:.2f}% | "
          f"胜率: {s['win_rate_pct']:.1f}% | PF: {s['profit_factor']}")
    print(f"交易: {s['trade_count']}笔 | 期末权益: {s['final_equity']:,.0f}")

    # 逐年
    by_year = defaultdict(lambda: {"t": 0, "w": 0, "p": 0.0})
    for t in result["trades"]:
        if t.get("sell_reason") == "期末": continue
        yr = t["buy_date"][:4]
        by_year[yr]["t"] += 1
        if t["profit"] > 0: by_year[yr]["w"] += 1
        by_year[yr]["p"] += t["profit_pct"]
    print(f"\n逐年:")
    for yr in sorted(by_year):
        d = by_year[yr]
        print(f"  {yr}: {d['t']:>4}笔  胜率{d['w']/d['t']*100:.1f}%  累计{d['p']:+.1f}%")

    # 持仓
    positions = result.get("current_positions", [])
    if positions:
        print(f"\n当前持仓 ({len(positions)}个):")
        for p in positions:
            pnl = (p["cur_price"] / p["buy_price"] - 1) * 100
            print(f"  {p['name']:<16}  买{p['buy_date']} @{p['buy_price']:.1f}  "
                  f"现{p['cur_price']:.1f}  {pnl:+.1f}%")

    # Regime
    print(f"\nRegime分布:")
    regimes = [r["regime"] for r in result["regime_log"]]
    for reg in ["fast", "normal", "slow"]:
        c = regimes.count(reg)
        print(f"  {reg}: {c}/{len(regimes)} ({c/len(regimes)*100:.0f}%)", end="")
        if reg in result["regime_stats"]:
            rs = result["regime_stats"][reg]
            print(f"  → {rs['trades']}笔 胜率{rs['win_rate']}% 均盈{rs['avg_pnl']}%")
        else:
            print()

    # 保存
    out_dir = ROOT_DIR / "data" / "strategy" / "divergent_final"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{stamp}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n已保存: {out_path}")


if __name__ == "__main__":
    main()
