#!/usr/bin/env python3
"""从趋势跟随回测缓存提取 6/22 持仓"""
import json, sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

cache_file = ROOT_DIR / "data" / "strategy" / "trend_following" / "2026-06" / "2026-06-22_4.json"
with open(cache_file, encoding="utf-8") as f:
    data = json.load(f)

# equity_curve 最后几天
eq = data["equity_curve"]
print("=== equity_curve 最后 5 天 ===")
for e in eq[-5:]:
    print(f"  {e['date']}  净值{e['equity']:,.0f}  现金{e['cash']:,.0f}  持仓{e['position_count']}只")

# trades 里面 sell_reason == "期末持仓" = 回测结束时仍在持有
open_trades = [t for t in data["trades"] if t.get("sell_reason") == "期末持仓"]

print(f"\n=== 6/22 策略持仓: {len(open_trades)} 只 (sell_reason=期末持仓) ===")
for t in open_trades:
    print(f"  {t['code']} {t['name']}  买入日{t['buy_date']}  买入价{t['buy_price']:.2f}  现价{t['sell_price']:.2f}  盈亏{t['profit_pct']:+.1f}%  原因:{t.get('buy_reason','')[:40]}")

# 同时写入 trade_history.json，让页面对比可用
history_file = ROOT_DIR / "data" / "trade_history.json"
with open(history_file, encoding="utf-8") as f:
    history = json.load(f)

# 把 strategy positions 写成 keeps（策略持有）
strategy_holds = []
for t in open_trades:
    strategy_holds.append({
        "code": t["code"],
        "name": t["name"],
        "shares": t.get("shares", 0),
        "buy_price": t.get("buy_price", 0),
        "current_price": t.get("current_price", t.get("buy_price", 0)),
        "profit_pct": t.get("profit_pct", 0),
        "buy_date": t.get("buy_date", ""),
    })

for h in history:
    if h["date"] == "2026-06-22":
        h["keeps"] = strategy_holds
        h["buy_signals"] = []  # 策略已有持仓时不需要新买
        break

with open(history_file, "w", encoding="utf-8") as f:
    json.dump(history, f, ensure_ascii=False, indent=2)
print(f"\n已写入 trade_history.json (策略持仓 {len(strategy_holds)} 只 → keeps)")
