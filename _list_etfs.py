"""列出当前105只ETF + 全量列表按类别分析"""
import json, csv, os

ROOT = os.path.dirname(os.path.abspath(__file__))

# 当前有数据的105只
with open(os.path.join(ROOT, "data/etf/2026/2026-07.csv"), "r", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))
tracked = sorted(set(r["code"] for r in rows))
print(f"=== 当前有数据的 {len(tracked)} 只ETF ===")
for code in tracked:
    name = next((r["name"] for r in rows if r["code"] == code), "?")
    print(f"  {code} {name}")

# 全量列表
with open(os.path.join(ROOT, "data", "etf_codes.json"), "r", encoding="utf-8") as f:
    all_etfs = json.load(f)
print(f"\n=== 全量 {len(all_etfs)} 只 ===")

# 按代码前缀分类
from collections import Counter
prefixes = Counter(e["code"][:2] for e in all_etfs)
print(f"sh: {prefixes.get('sh', 0)} 只  sz: {prefixes.get('sz', 0)} 只")

# 看看前200只都是什么
print(f"\n=== 全量前50只 ===")
for e in all_etfs[:50]:
    in_data = "[OK]" if e["code"] in tracked else "[--]"
    print(f"  {in_data} {e['code']} {e['name']}")
