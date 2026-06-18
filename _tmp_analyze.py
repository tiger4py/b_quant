# -*- coding: utf-8 -*-
"""临时分析脚本：拆解当前 volatility_breakout 回测结果，找出优化方向。"""
import sqlite3
import json
import statistics
from collections import Counter

c = sqlite3.connect('data/stock.db')
cur = c.cursor()
cur.execute("SELECT result_json FROM backtest_cache WHERE cache_key='volatility_breakout_market_1000_pos5'")
row = cur.fetchone()
data = json.loads(row[0])

print('=== SUMMARY ===')
for k, v in data['summary'].items():
    print(f'{k}: {v}')
print()
print('=== SELECTION ===')
for k, v in data['selection'].items():
    print(f'{k}: {v}')
print()

trades = data['trades']
print('=== TRADE COUNT ===', len(trades))
wins = [t for t in trades if t['profit'] > 0]
losses = [t for t in trades if t['profit'] <= 0]
print(f'wins={len(wins)} losses={len(losses)}')
print('avg profit pct:', round(statistics.mean([t["profit_pct"] for t in trades]), 2))
print('median profit pct:', round(statistics.median([t["profit_pct"] for t in trades]), 2))
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
    print(f'{r}: n={len(lst)} avg={statistics.mean(lst):.2f}% sum={sum(lst):.1f}')
print()

print('=== TOP 10 WINNERS ===')
for t in sorted(trades, key=lambda x: -x['profit'])[:10]:
    print(f"{t['code']} {t['name']} {t['buy_date']}->{t['sell_date']} profit={t['profit_pct']:.2f}% reason={t['sell_reason']}")
print()
print('=== TOP 10 LOSERS ===')
for t in sorted(trades, key=lambda x: x['profit'])[:10]:
    print(f"{t['code']} {t['name']} {t['buy_date']}->{t['sell_date']} profit={t['profit_pct']:.2f}% reason={t['sell_reason']}")
print()

# 持仓天数分布
hold_days = []
for t in trades:
    from datetime import datetime
    try:
        d1 = datetime.strptime(t['buy_date'], '%Y-%m-%d')
        d2 = datetime.strptime(t['sell_date'], '%Y-%m-%d')
        hold_days.append((d2 - d1).days)
    except Exception:
        pass
print('=== HOLD DAYS ===')
print(f'avg={statistics.mean(hold_days):.1f} median={statistics.median(hold_days):.1f} max={max(hold_days)} min={min(hold_days)}')
print()

# 按年份分组
year_stats = {}
for t in trades:
    y = t['buy_date'][:4]
    year_stats.setdefault(y, []).append(t['profit_pct'])
print('=== BY YEAR ===')
for y in sorted(year_stats):
    lst = year_stats[y]
    w = sum(1 for x in lst if x > 0)
    print(f'{y}: n={len(lst)} avg={statistics.mean(lst):.2f}% win_rate={w/len(lst)*100:.1f}% sum={sum(lst):.1f}')
