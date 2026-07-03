"""扫描所有 ETF 最新 Alpha042 买卖信号 + 近期推荐"""
import os, sys, csv, json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backtest.strategy.strategy_etf_alpha042 import generate_signals, _compute_metrics

# ======== 加载 ETF 数据 ========
ETF_DIR = ROOT / "data" / "etf"
CROSS_KW = ["港股","恒生","纳指","标普","日经","中概","H股","跨境","德国","法国","越南","印度"]

bars_by_code = defaultdict(list)
for root, dirs, files in os.walk(ETF_DIR):
    for f in files:
        if not f.endswith(".csv"): continue
        with open(os.path.join(root, f), "r", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                bars_by_code[row["code"]].append({
                    "trade_date": row["trade_date"],
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": int(row["volume"]), "amount": float(row["amount"]),
                    "name": row.get("name", ""),
                })

# 过滤跨境
name_lookup = {}
with open(ROOT / "data" / "etf_codes_main.json", "r", encoding="utf-8") as f:
    for e in json.load(f):
        name_lookup[e["code"]] = e["name"]

valid = {}
for code, bars in bars_by_code.items():
    name = name_lookup.get(code, "")
    if any(kw in name for kw in CROSS_KW): continue
    bars.sort(key=lambda b: b["trade_date"])
    if len(bars) < 200: continue
    valid[code] = bars

print(f"有效 ETF: {len(valid)} 只\n")

# ======== 逐个扫描信号 ========
results = []

for code, bars in sorted(valid.items()):
    name = name_lookup.get(code, code)
    signals = generate_signals(bars)

    if not signals:
        continue

    # 找最后一组买卖信号
    last_buy = None
    last_sell = None
    for s in signals:
        if s["action"] == "buy":
            last_buy = s
            last_sell = None  # 新的买入重置卖出
        elif s["action"] == "sell":
            last_sell = s

    # 计算当前指标
    m = _compute_metrics(bars)
    n = len(bars)
    latest = bars[-1]
    # 找最新的有效指标
    corr_val = None; vol_amp = None; h20 = None; chg5 = None
    for i in range(n-1, -1, -1):
        if corr_val is None and m["high_vol_corr"][i] is not None:
            corr_val = m["high_vol_corr"][i]
        if vol_amp is None and m["vol_amp"][i] is not None:
            vol_amp = m["vol_amp"][i]
        if h20 is None and m["high_20"][i] is not None:
            h20 = m["high_20"][i]
        if chg5 is None and m["chg_5d"][i] is not None:
            chg5 = m["chg_5d"][i]
        if all(x is not None for x in [corr_val, vol_amp, h20, chg5]):
            break

    close = latest["close"]
    date = latest["trade_date"]

    # 判断状态
    holding = (last_buy and not last_sell)  # 当前持有（有买无卖）

    # 距买入盈亏
    buy_pnl = None
    if holding and last_buy:
        # 找买入日收盘价
        buy_bar = next((b for b in bars if b["trade_date"] == last_buy["date"]), None)
        if buy_bar:
            buy_pnl = (close / buy_bar["close"] - 1) * 100

    # 评分（用于排序推荐）
    score = 0
    if corr_val is not None:
        # corr 越负越好（-0.5 比 -0.25 分高）
        score += max(0, (-corr_val - 0.25) * 30)
    if vol_amp is not None and 1.2 <= vol_amp <= 5:
        score += 15
    if h20 is not None:
        dist = (close / h20 - 1) * 100
        if -5 <= dist <= 0:  # 接近20日高
            score += 20
        elif 0 <= dist <= 5:
            score += 10
    if chg5 is not None and chg5 > -0.05:
        score += 15
    if holding:
        score += 25  # 已有持仓持有加分

    results.append({
        "code": code, "name": name, "date": date, "close": close,
        "corr": corr_val, "vol_amp": vol_amp, "h20_dist": (close/h20-1)*100 if h20 else None,
        "chg5": chg5*100 if chg5 else None,
        "holding": holding, "buy_pnl": buy_pnl,
        "last_buy_date": last_buy["date"] if last_buy else None,
        "score": round(score, 1),
    })

# ======== 输出 ========

# 1. 当前持仓（有买无卖）
holding_list = [r for r in results if r["holding"]]
holding_list.sort(key=lambda r: -r["score"])

print("=" * 75)
print(f"[1] 当前 Alpha042 持仓信号 ({len(holding_list)} 只)")
print("=" * 75)
if holding_list:
    print(f"  {'代码':12s} {'名称':22s} {'现价':>8s} {'盈亏':>8s} {'corr':>8s} {'波放':>6s} {'距20高':>8s} {'买入日'}")
    print("  " + "-" * 70)
    for r in holding_list:
        pnl = f"{r['buy_pnl']:+.1f}%" if r['buy_pnl'] is not None else "-"
        corr_s = f"{r['corr']:.3f}" if r['corr'] else "-"
        va_s = f"{r['vol_amp']:.1f}x" if r['vol_amp'] else "-"
        h20_s = f"{r['h20_dist']:+.1f}%" if r['h20_dist'] is not None else "-"
        print(f"  {r['code']:12s} {r['name']:22s} {r['close']:>8.2f} {pnl:>8s} {corr_s:>8s} {va_s:>6s} {h20_s:>8s} {r['last_buy_date']}")

else:
    print("  无当前持仓信号")

# 2. 接近买入的（缩量背离但还没触发）
near_buy = [r for r in results if not r["holding"] and r["corr"] is not None and r["corr"] < 0]
near_buy.sort(key=lambda r: r["corr"])  # 最负的排前面

print(f"\n{'=' * 75}")
print(f"[2] 缩量背离中 (corr<0, 未触发买入) — {len(near_buy)} 只")
print("=" * 75)
if near_buy:
    print(f"  {'代码':12s} {'名称':22s} {'现价':>8s} {'corr':>8s} {'波放':>6s} {'距20高':>8s} {'5日%':>8s} {'评分'}")
    print("  " + "-" * 70)
    for r in near_buy[:20]:
        corr_s = f"{r['corr']:.3f}" if r['corr'] else "-"
        va_s = f"{r['vol_amp']:.1f}x" if r['vol_amp'] else "-"
        h20_s = f"{r['h20_dist']:+.1f}%" if r['h20_dist'] is not None else "-"
        c5_s = f"{r['chg5']:+.1f}%" if r['chg5'] is not None else "-"
        print(f"  {r['code']:12s} {r['name']:22s} {r['close']:>8.2f} {corr_s:>8s} {va_s:>6s} {h20_s:>8s} {c5_s:>8s} {r['score']:>5.1f}")

# 3. 综合推荐：持有 + 最近要买的
print(f"\n{'=' * 75}")
print(f"[3] 综合推荐 (评分排序)")
print("=" * 75)

# 全部排序
results.sort(key=lambda r: -r["score"])
print(f"  {'排名':>4s} {'代码':12s} {'名称':22s} {'现价':>8s} {'corr':>8s} {'状态':10s} {'评分'}")
print("  " + "-" * 70)
for rank, r in enumerate(results[:25], 1):
    corr_s = f"{r['corr']:.3f}" if r['corr'] else "-"
    state = "持有" if r["holding"] else ("背离中" if (r["corr"] or 0) < 0 else "观望")
    pnl_s = f"({r['buy_pnl']:+.1f}%)" if r['buy_pnl'] is not None else ""
    print(f"  {rank:>4d} {r['code']:12s} {r['name']:22s} {r['close']:>8.2f} {corr_s:>8s} {state + pnl_s:16s} {r['score']:>5.1f}")

# 4. 分类推荐
print(f"\n{'=' * 75}")
print(f"[4] 按类别推荐")
print("=" * 75)
# 从回测结果找盈利的类别
good_cats = {
    "宽基": ["沪深300", "中证500", "中证1000", "中证2000", "上证50", "创业板", "科创50", "A500"],
    "半导体": ["芯片", "半导体"],
    "黄金": ["黄金"],
    "红利": ["红利", "低波"],
}

for cat, kws in good_cats.items():
    cat_items = [r for r in results if any(kw in r["name"] for kw in kws) and r["score"] > 0]
    cat_items.sort(key=lambda r: -r["score"])
    if cat_items:
        top = cat_items[:3]
        line = ", ".join(f"{r['code']} {r['name']}(score={r['score']:.0f})" for r in top)
        print(f"  {cat}: {line}")
