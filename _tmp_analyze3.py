# -*- coding: utf-8 -*-
"""分析优化后回测结果。"""
import sqlite3
import json
import statistics
from collections import Counter
from datetime import datetime

c = sqlite3.connect('data/stock.db')
cur = c.cursor()
cur.execute("SELECT result_json FROM backtest_cache WHERE cache_key='volatility_breakout_market_1000_pos5'")
data = json.loads(cur.fetchone()[0])
trades = data['trades']

print('=== SELL REASON DISTRIBUTION ===')
reasons = Counter(t['sell_reason'].split('(')[0].strip() for t in trades)
for r, n in reasons.most_common():
    print(f'{r}: {n}')
print()

print('=== SELL REASON - avg profit ===')
by_reason = {}
for t in trades:
    key = t['sell_reason'].split('(')[0].strip()
    by_reason.setdefault(key, []).append(t['profit_pct'])
for r, lst in sorted(by_reason.items(), key=lambda x: -statistics.mean(x[1])):
    w = sum(1 for x in lst if x > 0)
    print(f'{r}: n={len(lst)} avg={statistics.mean(lst):.2f}% win={w/len(lst)*100:.1f}% sum={sum(lst):.1f}')
print()

# 持仓天数
buckets = {'1-2': [], '3-5': [], '6-10': [], '11-15': [], '>15': []}
for t in trades:
    try:
        d1 = datetime.strptime(t['buy_date'], '%Y-%m-%d')
        d2 = datetime.strptime(t['sell_date'], '%Y-%m-%d')
        h = (d2 - d1).days
    except Exception:
        continue
    if h <= 2:
        buckets['1-2'].append(t['profit_pct'])
    elif h <= 5:
        buckets['3-5'].append(t['profit_pct'])
    elif h <= 10:
        buckets['6-10'].append(t['profit_pct'])
    elif h <= 15:
        buckets['11-15'].append(t['profit_pct'])
    else:
        buckets['>15'].append(t['profit_pct'])
print('=== HOLD DAYS BUCKETS ===')
for k, lst in buckets.items():
    if not lst:
        continue
    w = sum(1 for x in lst if x > 0)
    print(f'{k}天: n={len(lst)} avg={statistics.mean(lst):.2f}% win={w/len(lst)*100:.1f}% sum={sum(lst):.1f}')

# 单笔最大亏损
print()
print('=== TOP 10 LOSERS ===')
for t in sorted(trades, key=lambda x: x['profit'])[:10]:
    print(f"{t['code']} {t['name']} {t['buy_date']}->{t['sell_date']} profit={t['profit_pct']:.2f}% reason={t['sell_reason']}")
