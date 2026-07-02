"""分析全量ETF列表，找出可筛选维度"""
import json, os
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))

# 直接用 akshare 重新拉一次，看完整字段
import akshare as ak
df = ak.fund_etf_category_sina(symbol="ETF基金")

print(f"总列: {list(df.columns)}")
print(f"总行: {len(df)}")
print()

# 看看前5行
print("=== 前5行 ===")
print(df.head().to_string())
print()

# 按类别统计
if "类别" in df.columns:
    cats = df["类别"].value_counts()
    print(f"=== 类别分布 (共{len(cats)}类) ===")
    for cat, cnt in cats.items():
        print(f"  {cat}: {cnt} 只")

# 如果有规模/成交额字段
for col in ["规模", "基金规模", "日均成交额", "成交额"]:
    if col in df.columns:
        print(f"\n=== {col} ===")
        print(df[col].describe())
