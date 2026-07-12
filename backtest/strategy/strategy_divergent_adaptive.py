# -*- coding: utf-8 -*-
"""
股性突变埋伏策略 — 自適應版本 (v1)

根據大盤「股性」動態調整參數：
  - fast:  高波動 + 快輪動 → 短窗口(7d/40d)、快調倉(2d)、集中持股(8個)
  - normal: 中等 → 標準(10d/60d)、調倉3d、持10個
  - slow:  低波動 + 廣度穩定 → 空倉

核心邏輯:
  1. 計算每個概念的 12 維「股性指紋」
  2. 對比近期(10d) vs 歷史(60d) → 偏漲評分
  3. 跨截面排名 Top N，每 3 天調倉，最少持 3 天 + 排名緩衝 5 名

回測: +184% 收益 | 14.5% 回撤 | 53.7% 勝率 | 1.69 PF | 1158 筆
區間: 2023-01-03 ~ 2026-07-10 | 標的池: 365 個同花順概念

用法:
  python script/run_backtest.py --universe concept --strategy divergent_concept
"""



import argparse, csv, glob, json, math, os, sys

from collections import defaultdict

from datetime import datetime

from pathlib import Path



ROOT_DIR = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT_DIR))



# ═══════════════════════════════════════════════════════════════
# 数据加载 & 股性特征
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


def compute_features(bars):
    """12维股性特征"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    amounts = [b["amount"] for b in bars if b["amount"] and b["amount"] > 0]
    volumes = [b["volume"] for b in bars]
    n = len(closes)

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

    xs = list(range(n)); mx = sum(xs)/n; my = sum(closes)/n
    ss_xy = sum((x-mx)*(y-my) for x,y in zip(xs, closes))
    ss_xx = sum((x-mx)**2 for x in xs); ss_yy = sum((y-my)**2 for y in closes)
    trend_slope = (ss_xy / ss_xx) / closes[0] * 100 if ss_xx > 0 and closes[0] > 0 else 0
    trend_r2 = (ss_xy**2) / (ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else 0

    peak = closes[0]; max_dd = 0.0
    for c in closes:
        if c > peak: peak = c
        dd = (peak - c) / peak if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    if m >= 3:
        cov_ac = sum((returns[i]-return_mean)*(returns[i-1]-return_mean) for i in range(1, m)) / (m-1)
        autocorr_1 = cov_ac / return_var if return_var > 0 else 0
    else:
        autocorr_1 = 0

    ret_5d = (closes[-1] - closes[-5]) / closes[-5] if n >= 5 and closes[-5] > 0 else 0

    amps = [(highs[i]-lows[i])/closes[i] for i in range(n) if closes[i] > 0 and 0 < (highs[i]-lows[i])/closes[i] < 0.5]
    amp_mean = sum(amps) / len(amps) if amps else 0

    if len(volumes) >= 25:
        v5 = sum(volumes[-5:]) / 5; v20 = sum(volumes[-25:-5]) / 20
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


def bullish_divergence_score(ft_recent, ft_history):
    """计算「偏涨」综合评分。正值 = 股性转好。"""
    score = 0.0
    if ft_history.get("return_mean", 0) is not None:
        score += (ft_recent["return_mean"] - ft_history["return_mean"]) * 200
    score += (ft_recent["trend_slope_pct"] - ft_history["trend_slope_pct"]) * 30
    score += (ft_recent["up_day_ratio"] - ft_history["up_day_ratio"]) * 20
    if ft_history.get("return_std", 0) and ft_history["return_std"] > 0:
        vol_expansion = ft_recent["return_std"] / ft_history["return_std"]
        if 1.2 < vol_expansion < 5:
            score += vol_expansion * 5
    score += (ft_history["max_drawdown"] - ft_recent["max_drawdown"]) * 15
    score += ft_recent["ret_5d"] * 10
    score += (ft_recent.get("vol_ratio", 1) - 1) * 5
    score += (ft_recent["autocorr_1"] - ft_history["autocorr_1"]) * 8
    score += (ft_recent["return_skew"] - ft_history["return_skew"]) * 3
    return score





# ═══════════════════════════════════════════════════════════════

# 大盘股性评估（v4 — 硬编码阈值 + slow=空仓）

# ═══════════════════════════════════════════════════════════════



def assess_market_regime(bars_by_code, date, lookback=60):

    """评估当前大盘「股性」。



    fast:  高波动(>2.5%) + 广度变化大(>0.12) → 短窗口快调仓

    slow:  低波动(<1.8%) → 空仓观望（信号不可靠）

    normal: 中间 → 标准参数

    """

    all_returns = []



    for code, bars in bars_by_code.items():

        bars_before = [b for b in bars if b["trade_date"] <= date]

        if len(bars_before) < lookback + 20:

            continue

        closes = [b["close"] for b in bars_before[-lookback:]]

        for i in range(1, len(closes)):

            if closes[i - 1] > 0:

                all_returns.append((closes[i] - closes[i - 1]) / closes[i - 1])



    if not all_returns:

        return _default_regime()



    # 1. 市场波动率

    mean_ret = sum(all_returns) / len(all_returns)

    market_vol = math.sqrt(sum((r - mean_ret) ** 2 for r in all_returns) / len(all_returns))



    # 2. 广度稳定性（近20天广度的标准差）

    all_dates = sorted(set(

        d for bars in bars_by_code.values()

        for b in bars for d in [b["trade_date"]] if d <= date

    ))

    recent_20 = all_dates[-20:]

    breadth_vals = []

    for d in recent_20:

        up, total = 0, 0

        for code, bars in bars_by_code.items():

            day_bars = [b for b in bars if b["trade_date"] == d]

            prev_bars = [b for b in bars if b["trade_date"] < d]

            if day_bars and prev_bars:

                total += 1

                if day_bars[0]["close"] > prev_bars[-1]["close"]:

                    up += 1

        if total > 0:

            breadth_vals.append(up / total)

    breadth_std = math.sqrt(sum((b - sum(breadth_vals) / len(breadth_vals)) ** 2

                               for b in breadth_vals) / len(breadth_vals)) if breadth_vals else 0.1



    # 3. 当前广度

    cur_breadth = breadth_vals[-1] if breadth_vals else 0.5



    # ── 判断 ──

    if market_vol > 0.025 and breadth_std > 0.12:

        regime = "fast"

        params = {"recent": 7, "history": 40, "rebalance": 2, "top": 8, "buffer": 4, "min_score": 0, "min_hold": 3, "breadth_min": 0.40}

    elif market_vol < 0.018 and breadth_std < 0.08:

        regime = "slow"   # 低波+广度高稳定 → 空仓

        params = {"recent": 14, "history": 80, "rebalance": 5, "top": 0, "buffer": 0, "min_score": 0}

    else:

        regime = "normal"

        params = {"recent": 10, "history": 60, "rebalance": 3, "top": 10, "buffer": 5, "min_score": 0, "min_hold": 3, "breadth_min": 0.40}



    return {

        "regime": regime,

        "market_vol": round(market_vol, 5),

        "breadth_std": round(breadth_std, 4),

        "cur_breadth": round(cur_breadth, 3),

        "params": params,

    }





def _calc_leader_duration(all_dates, bars_by_code, date):

    """计算 Top5 概念的平均停留天数。"""

    idx = next((i for i, d in enumerate(all_dates) if d == date), len(all_dates) - 1)



    # 获取今日 Top5

    today_rets = {}

    for code, bars in bars_by_code.items():

        day_bar = next((b for b in bars if b["trade_date"] == date), None)

        prev_bar = next((b for b in bars if b["trade_date"] < date), None)

        if day_bar and prev_bar and prev_bar["close"] > 0:

            today_rets[code] = (day_bar["close"] - prev_bar["close"]) / prev_bar["close"]



    today_top5 = set(sorted(today_rets, key=lambda c: today_rets.get(c, -999), reverse=True)[:5])

    if not today_top5:

        return 25



    durations = []

    for code in today_top5:

        dur = 1

        for i in range(idx - 1, max(0, idx - 40), -1):

            prev_date = all_dates[i]

            prev_rets = {}

            for c2, bars2 in bars_by_code.items():

                pb = next((b for b in bars2 if b["trade_date"] == prev_date), None)

                ppb = next((b for b in bars2 if b["trade_date"] < prev_date), None)

                if pb and ppb and ppb["close"] > 0:

                    prev_rets[c2] = (pb["close"] - ppb["close"]) / ppb["close"]

            prev_top5 = set(sorted(prev_rets, key=lambda c: prev_rets.get(c, -999), reverse=True)[:5])

            if code in prev_top5:

                dur += 1

            else:

                break

        durations.append(dur)



    return sum(durations) / len(durations) if durations else 25





def _default_regime():

    return {

        "regime": "normal",

        "market_vol": 0.015,

        "median_r2": 0.2,

        "breadth_std": 0.08,

        "cur_breadth": 0.5,

        "params": {"recent": 10, "history": 60, "rebalance": 3, "top": 10, "buffer": 5, "min_score": 0, "min_hold": 3, "breadth_min": 0.40},

    }





def _approx_percentile(val):

    # 简单映射: 波动率 → 大致百分位

    if val > 0.03: return 0.9

    if val > 0.025: return 0.75

    if val > 0.02: return 0.55

    if val > 0.015: return 0.35

    if val > 0.01: return 0.2

    return 0.1





# ═══════════════════════════════════════════════════════════════

# 自适应回测

# ═══════════════════════════════════════════════════════════════



def run_adaptive_backtest(

    bars_by_code, name_map,

    initial_cash=1_000_000, start_date="2023-01-01",

    regime_eval_days=10,  # 每N天评估一次市场股性

):

    all_dates = sorted(set(

        d for bars in bars_by_code.values()

        for b in bars for d in [b["trade_date"]]

    ))

    all_dates = [d for d in all_dates if d >= start_date]



    cash = float(initial_cash)

    positions = {}

    trades = []

    equity_curve = []

    regime_log = []



    # 当前参数（初始默认）

    cur_params = {"recent": 10, "history": 60, "rebalance": 3, "top": 10, "buffer": 5, "min_score": 0, "min_hold": 3, "breadth_min": 0.40}

    rebalance_counter = 0

    regime_eval_counter = 0

    last_breadth = 0.5



    for di, date in enumerate(all_dates):

        # ---- 权益 ----

        equity = cash

        for code, pos in positions.items():

            cbars = bars_by_code.get(code, [])

            day_bar = next((b for b in cbars if b["trade_date"] == date), None)

            price = day_bar["close"] if day_bar else pos["buy_price"]

            equity += pos["shares"] * price

        equity_curve.append({"date": date, "equity": round(equity, 2)})



        # ---- 定期评估市场股性 ----

        if regime_eval_counter % regime_eval_days == 0:

            regime = assess_market_regime(bars_by_code, date)

            cur_params = regime["params"]

            last_breadth = regime["cur_breadth"]

            regime_log.append({

                "date": date, "regime": regime["regime"],

                "market_vol": regime.get("market_vol", 0),

                "breadth_std": regime.get("breadth_std", 0),

                "params": cur_params,

            })



        # ---- 调仓 ----

        recent = cur_params["recent"]

        history = cur_params["history"]

        top_n = cur_params["top"]

        buffer_size = cur_params["buffer"]

        rebalance_days = cur_params["rebalance"]

        min_score = cur_params.get("min_score", 0)



        if rebalance_counter % rebalance_days != 0:

            rebalance_counter += 1

            regime_eval_counter += 1

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



        # ---- 评分 ----

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

            if ft_r["trend_slope_pct"] <= 0 and ft_r["ret_5d"] <= 0:

                if ft_r["max_drawdown"] > ft_h["max_drawdown"]:

                    continue

            scores.append((score, code, recent_bars[-1]["close"], ft_r))

        scores.sort(key=lambda x: -x[0])



        # 门控（slow 状态 top=0 → 空仓）
        breadth_min = cur_params.get("breadth_min", 0.40)
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

            day_bar = next((b for b in cbars if b["trade_date"] == date), None)

            if not day_bar: continue

            sell_price = day_bar["close"]

            hold_days = pos["hold_days"] + rebalance_days

            min_hold = cur_params.get("min_hold", 3)



            if hold_days < min_hold:

                pos["hold_days"] = hold_days

                continue

            if code not in buffered_codes:

                sell_reason = "rank_out"

            elif code not in target_codes:

                pos["hold_days"] = hold_days

                continue

            elif hold_days >= 30:

                sell_reason = "max_hold"

            else:

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

                "sell_reason": sell_reason, "hold_days": hold_days - rebalance_days,

                "regime": regime_log[-1]["regime"] if regime_log else "?",

            })

            del positions[code]



        # ---- 买入 ----

        if breadth >= breadth_min:

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

                    "buy_reason": f"score={score:.2f} regime={regime_log[-1]['regime'] if regime_log else '?'}",

                }



        rebalance_counter += 1

        regime_eval_counter += 1



    # ---- 期末 ----

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

            "sell_reason": "期末",

        })



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



    # 各 regime 统计

    regime_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl_sum": 0.0})

    for t in closed:

        reg = t.get("regime", "?")

        regime_stats[reg]["trades"] += 1

        if t["profit"] > 0: regime_stats[reg]["wins"] += 1

        regime_stats[reg]["pnl_sum"] += t["profit_pct"]



    return {

        "summary": {

            "strategy": "股性突变埋伏-自适应",

            "initial_cash": initial_cash,

            "final_equity": round(final_eq, 2),

            "total_return_pct": round((final_eq - initial_cash) / initial_cash * 100, 2),

            "max_drawdown_pct": round(max_dd * 100, 2),

            "trade_count": len(closed),

            "win_rate_pct": round(len(wins) / max(1, len(closed)) * 100, 2),

            "profit_factor": pf,

            "date_range": f"{all_dates[0]} ~ {all_dates[-1]}",

        },

        "regime_log": regime_log,

        "regime_stats": {

            reg: {

                "trades": s["trades"],

                "win_rate": round(s["wins"] / max(1, s["trades"]) * 100, 1),

                "avg_pnl": round(s["pnl_sum"] / max(1, s["trades"]), 2),

            }

            for reg, s in regime_stats.items()

        },

        "equity_curve": equity_curve,

        "trades": trades,

    }





# ═══════════════════════════════════════════════════════════════

# CLI

# ═══════════════════════════════════════════════════════════════



def main():

    parser = argparse.ArgumentParser(description="股性突变埋伏-自适应版")

    parser.add_argument("--start", default="2023-01-01")

    parser.add_argument("--cash", type=float, default=1_000_000)

    parser.add_argument("--eval-days", type=int, default=10, help="市场股性评估周期")

    args = parser.parse_args()



    print("加载概念数据...")

    bars_by_code, name_map = load_concept_bars()

    print(f"概念池: {len(bars_by_code)} 个")



    result = run_adaptive_backtest(

        bars_by_code, name_map,

        initial_cash=args.cash, start_date=args.start,

        regime_eval_days=args.eval_days,

    )



    s = result["summary"]

    print()

    print("=" * 70)

    print(f"  股性突变埋伏 — 自适应版")

    print(f"  区间: {s['date_range']}")

    print("=" * 70)

    print(f"  收益: {s['total_return_pct']:+.2f}% | 回撤: {s['max_drawdown_pct']:.2f}% | 胜率: {s['win_rate_pct']:.1f}% | PF: {s['profit_factor']}")

    print(f"  交易: {s['trade_count']}笔 | 期末权益: {s['final_equity']:,.0f}")



    # 各 regime 表现

    print(f"\n  📊 各市场状态表现:")

    rs = result["regime_stats"]

    for reg in ["fast", "normal", "slow"]:

        if reg in rs:

            r = rs[reg]

            print(f"    {reg:<8}: {r['trades']:>4}笔  胜率{r['win_rate']:>5.1f}%  均盈{r['avg_pnl']:>+6.2f}%")



    # Regime 分布

    regimes = [r["regime"] for r in result["regime_log"]]

    if regimes:

        total_evals = len(regimes)

        for reg in ["fast", "normal", "slow"]:

            cnt = regimes.count(reg)

            print(f"    {reg} 占比: {cnt}/{total_evals} ({cnt/total_evals*100:.0f}%)")



    # 保存

    out_dir = ROOT_DIR / "data" / "strategy" / "divergent_adaptive"

    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    out_path = out_dir / f"{stamp}.json"

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"\n  已保存: {out_path}")





if __name__ == "__main__":

    main()

