"""统计各月ETF数据覆盖情况"""
import csv, os

ROOT = os.path.dirname(os.path.abspath(__file__))

for m in range(1, 8):
    fp = os.path.join(ROOT, f"data/etf/2026/2026-{m:02d}.csv")
    if not os.path.exists(fp):
        continue
    with open(fp, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    codes = set(r["code"] for r in rows)
    dates_set = set(r["trade_date"] for r in rows)
    print(f"2026-{m:02d}: {len(rows)}行, {len(codes)}只ETF, {len(dates_set)}个交易日, "
          f"日均{len(rows)/max(1,len(dates_set)):.0f}条")

    # 每日明细
    from collections import Counter
    day_counts = Counter(r["trade_date"] for r in rows)
    for d in sorted(day_counts):
        print(f"    {d}: {day_counts[d]} 条")
