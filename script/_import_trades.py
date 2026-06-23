#!/usr/bin/env python3
"""导入6/22趋势跟随5只候选股到实盘持仓"""
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from main import app

c = app.test_client()

# 6/22 趋势跟随选出的5只，等权分配约8万/只
buys = [
    ("sz.002303", 3.68, 21700, "2026-06-23"),
    ("sz.002734", 18.94, 4200, "2026-06-23"),
    ("sz.001216", 17.95, 4400, "2026-06-23"),
    ("sz.002367", 5.98, 13300, "2026-06-23"),
    ("sh.603068", 37.46, 2100, "2026-06-23"),
]

for code, price, shares, date in buys:
    r = c.post("/api/trading/buy", json={
        "code": code, "price": price, "shares": shares, "date": date
    })
    print(r.get_json()["ok"])

# 查状态
r = c.get("/api/trading/state")
d = r.get_json()
print(f"\n现金剩余: {d['portfolio']['cash']:,.0f}")
print(f"持仓: {len(d['holdings'])} 只")
for h in d["holdings"]:
    print(f"  {h['code']} {h['name']} {h['shares']}股 @ {h['buyPrice']}  市值{h['marketValue']:,.0f}  现价{h['currentPrice']}  盈亏{h['pnlPct']:+.1f}%")
print(f"浮动盈亏: {d['stats']['totalPnl']:+,.0f}")
