"""按成交额筛选主流ETF，生成精简列表"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.abspath(__file__))

import akshare as ak
df = ak.fund_etf_category_sina(symbol="ETF基金")

# 成交额 > 5亿
MIN_AMOUNT = 500_000_000  # 5亿
df["成交额"] = df["成交额"].astype(float)
main = df[df["成交额"] > MIN_AMOUNT].copy()

print(f"全量: {len(df)} 只")
print(f"成交额 > 5亿: {len(main)} 只 ({len(main)/len(df)*100:.1f}%)")

# 按成交额降序
main = main.sort_values("成交额", ascending=False)

# 排除债券/货币类ETF
BOND_KEYWORDS = ["债", "货币", "短融", "城投", "转债", "可转债",
                  "日利", "添益", "保证金", "理财金"]
before = len(main)
main = main[~main["名称"].str.contains("|".join(BOND_KEYWORDS))]
print(f"排除债券/货币: {before} → {len(main)} 只")

# 分类统计
from collections import Counter
keywords = ["沪深300", "中证500", "中证1000", "中证2000", "上证50", "创业板", "科创",
            "证券", "银行", "军工", "芯片", "半导体", "医药", "医疗", "酒",
            "新能源", "光伏", "5G", "计算机", "传媒", "游戏", "煤炭", "钢铁", "有色",
            "地产", "电力", "汽车", "黄金", "红利", "纳指", "恒生", "港股",
            "人工智能", "机器人", "通信", "消费"]
cat = Counter()
for name in main["名称"]:
    found = [kw for kw in keywords if kw in name]
    if found:
        cat[found[0]] += 1
    else:
        cat["其他"] += 1

print(f"\n=== 类别覆盖 ===")
for kw, cnt in cat.most_common(25):
    print(f"  {kw}: {cnt} 只")

# 看看哪些常见ETF没被覆盖
print(f"\n=== TOP 30 成交额 ===")
for _, row in main.head(30).iterrows():
    amt = row["成交额"] / 1e8
    print(f"  {row['代码']} {row['名称']:20s} 成交额:{amt:6.1f}亿")

# 保存
out_list = [{"code": row["代码"], "name": row["名称"]} for _, row in main.iterrows()]
out_path = os.path.join(ROOT, "data", "etf_codes_main.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out_list, f, ensure_ascii=False)
print(f"\n已保存 {len(out_list)} 只主流ETF → data/etf_codes_main.json")
