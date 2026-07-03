"""统计ETF数据年度分布"""
import os, csv
from collections import Counter, defaultdict

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "etf")

year_counts = Counter()
year_etfs = defaultdict(set)

for root, dirs, files in os.walk(ROOT):
    for f in files:
        if f.endswith('.csv'):
            with open(os.path.join(root, f), 'r', encoding='utf-8-sig') as fh:
                for row in csv.DictReader(fh):
                    yr = row['trade_date'][:4]
                    year_counts[yr] += 1
                    year_etfs[yr].add(row['code'])

print('年份    总行数    ETF数    日均')
for yr in sorted(year_counts):
    etf_cnt = len(year_etfs[yr])
    days = etf_cnt and year_counts[yr] / etf_cnt or 0
    print(f'{yr}    {year_counts[yr]:>6}    {etf_cnt:>4}     {days:>5.0f}')
print(f'总计   {sum(year_counts.values()):>6}')

# 2014年老ETF
print(f'\n2014年就有数据的ETF ({len(year_etfs.get("2014", set()))}只):')
for code in sorted(year_etfs.get('2014', set())):
    # 找名称
    name = "?"
    for root, dirs, files in os.walk(ROOT):
        for f in files:
            if f.endswith('.csv'):
                with open(os.path.join(root, f), 'r', encoding='utf-8-sig') as fh:
                    for row in csv.DictReader(fh):
                        if row['code'] == code:
                            name = row.get('name', '?')
                            break
                if name != '?':
                    break
        if name != '?':
            break
    print(f'  {code} {name}')
