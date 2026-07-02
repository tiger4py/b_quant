"""显示回测时间范围和年度收益"""
import os, sys, csv, json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backtest import get_strategy
from backtest.portfolio import run_portfolio_backtest

# 加载数据
bars_by_code = defaultdict(list)
for root, dirs, files in os.walk(ROOT / "data" / "etf"):
    for f in files:
        if f.endswith(".csv"):
            with open(os.path.join(root, f), "r", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    bars_by_code[row["code"]].append({
                        "trade_date": row["trade_date"],
                        "open": float(row["open"]), "high": float(row["high"]),
                        "low": float(row["low"]), "close": float(row["close"]),
                        "volume": int(row["volume"]), "amount": float(row["amount"]),
                    })

# 过滤
CROSS_KW = ["港股","恒生","纳指","标普","日经","中概","H股","跨境","德国","法国","越南","印度"]
stock_map = {}
with open(ROOT / "data" / "etf_codes_main.json", "r", encoding="utf-8") as f:
    etf_list = json.load(f)
name_lookup = {e["code"]: e["name"] for e in etf_list}

for code in list(bars_by_code.keys()):
    bars = sorted(bars_by_code[code], key=lambda b: b["trade_date"])
    name = name_lookup.get(code, "")
    if len(bars) < 200 or any(kw in name for kw in CROSS_KW):
        del bars_by_code[code]
    else:
        bars_by_code[code] = bars
        stock_map[code] = {"code": code, "name": name, "market": code[:2]}

# 回测
strategy = get_strategy("alpha042")
result = run_portfolio_backtest(
    bars_by_code=bars_by_code, stock_map=stock_map,
    strategy=strategy, initial_cash=1_000_000, max_positions=5,
)

curve = result["equity_curve"]
print(f"回测时间范围: {curve[0]['date']} ~ {curve[-1]['date']}")
print(f"交易日数: {len(curve)} 天")
print(f"初始权益: {curve[0]['equity']:,.0f}")
print(f"最终权益: {curve[-1]['equity']:,.0f}")
print(f"总收益率: {(curve[-1]['equity']/curve[0]['equity']-1)*100:+.2f}%")

# 年度收益
yearly = defaultdict(list)
for p in curve:
    yr = p["date"][:4]
    yearly[yr].append(p["equity"])

print()
for yr in sorted(yearly):
    vals = yearly[yr]
    ret = (vals[-1]/vals[0]-1)*100
    print(f"  {yr}年: {vals[0]:>10,.0f} -> {vals[-1]:>10,.0f}  ({ret:+.1f}%)")
