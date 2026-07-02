"""列出7月1号所有ETF"""
import csv, os

ROOT = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(ROOT, "data/etf/2026/2026-07.csv"), "r", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

# 只要7月1号
july1 = [r for r in rows if r["trade_date"] == "2026-07-01"]
july1.sort(key=lambda r: r["code"])

print(f"7月1号共 {len(july1)} 只ETF:\n")

# 按类别分组显示
for i, r in enumerate(july1):
    code = r["code"]
    name = r["name"]
    close = r["close"]
    vol = int(r["volume"])
    # 成交量转手/万手
    vol_wan = vol / 10000
    print(f"  {i+1:3d}. {code} {name:24s} close={float(close):>8.3f}  vol={vol_wan:>8.0f}万手")
