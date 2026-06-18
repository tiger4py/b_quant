# -*- coding: utf-8 -*-
"""分析第二次优化后的回测结果。"""
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

print('=== SUMMARY ===')
print(f'trades={len(trades)} return={data["summary"]["total_return_pct"]}% drawdown={data["summary"]["max_drawdown_pct"]}%')
print(f'win_rate={data["summary"]["win_rate_pct"]}% avg_profit={data["summary"]["avg_profit_pct"]}% pf={data["summary"]["profit_factor"]}')
print()

wins = [t for t in trades if t['profit'] > 0]
losses = [t for t in trades if t['profit'] <= 0]
print(f'wins={len(wins)} losses={len(losses)}')
print()

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
    if h <= 2: buckets['1-2'].append(t['profit_pct'])
    elif h <= 5: buckets['3-5'].append(t['profit_pct'])
    elif h <= 10: buckets['6-10'].append(t['profit_pct'])
    elif h <= 15: buckets['11-15'].append(t['profit_pct'])
    else: buckets['>15'].append(t['profit_pct'])
print('=== HOLD DAYS BUCKETS ===')
for k, lst in buckets.items():
    if not lst: continue
    w = sum(1 for x in lst if x > 0)
    print(f'{k}天: n={len(lst)} avg={statistics.mean(lst):.2f}% win={w/len(lst)*100:.1f}% sum={sum(lst):.1f}')
print()

# 波动率消退拆解
print('=== 波动率消退 detail ===')
volfade = [t for t in trades if '波动率消退' in t['sell_reason']]
for t in sorted(volfade, key=lambda x: x['profit_pct'])[:10]:
    print(f"  {t['code']} {t['name']} profit={t['profit_pct']:.2f}% {t['sell_reason']}")
print(f'  总计: n={len(volfade)} avg={statistics.mean([t["profit_pct"] for t in volfade]):.2f}%')

# 止损拆解
print()
print('=== 止损 detail ===')
stops = [t for t in trades if '止损' in t['sell_reason']]
for t in sorted(stops, key=lambda x: x['profit_pct'])[:10]:
    print(f"  {t['code']} {t['name']} profit={t['profit_pct']:.2f}% {t['buy_reason']}")

# V反失效拆解
print()
print('=== V反失效 detail ===')
vfail = [t for t in trades if 'V反失效' in t['sell_reason']]
print(f'  n={len(vfail)} avg={statistics.mean([t["profit_pct"] for t in vfail]):.2f}% sum={sum(t["profit_pct"] for t in vfail):.1f}')
