# -*- coding: utf-8 -*-
"""临时分析脚本 V2：拆解当前 volatility_breakout 回测结果。"""
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

# 持仓天数 vs 盈亏
print('=== HOLD DAYS BUCKETS ===')
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
for k, lst in buckets.items():
    if not lst:
        continue
    w = sum(1 for x in lst if x > 0)
    print(f'{k}天: n={len(lst)} avg={statistics.mean(lst):.2f}% win={w/len(lst)*100:.1f}% sum={sum(lst):.1f}')

# 波动率消退细节
print()
print('=== 波动率消退 detail (n=731) ===')
volfade = [t for t in trades if '波动率消退' in t['sell_reason']]
buckets2 = {'<-5': 0, '-5~-2': 0, '-2~0': 0, '0~2': 0, '2~5': 0, '5~8': 0, '>8': 0}
sums2 = {'<-5': 0, '-5~-2': 0, '-2~0': 0, '0~2': 0, '2~5': 0, '5~8': 0, '>8': 0}
for t in volfade:
    p = t['profit_pct']
    if p < -5:
        k = '<-5'
    elif p < -2:
        k = '-5~-2'
    elif p < 0:
        k = '-2~0'
    elif p < 2:
        k = '0~2'
    elif p < 5:
        k = '2~5'
    elif p < 8:
        k = '5~8'
    else:
        k = '>8'
    buckets2[k] += 1
    sums2[k] += p
for k in ['<-5', '-5~-2', '-2~0', '0~2', '2~5', '5~8', '>8']:
    print(f'{k}%: n={buckets2[k]} sum={sums2[k]:.1f}')

# 止损单分析
print()
print('=== 止损单 ===')
stops = [t for t in trades if '止损' in t['sell_reason']]
print(f'止损单数={len(stops)} avg_profit={statistics.mean([t["profit_pct"] for t in stops]):.2f}')
for t in sorted(stops, key=lambda x: x['profit'])[:8]:
    d1 = datetime.strptime(t['buy_date'], '%Y-%m-%d')
    d2 = datetime.strptime(t['sell_date'], '%Y-%m-%d')
    print(f"  {t['code']} {t['name']} 持仓{(d2-d1).days}天 {t['profit_pct']:.2f}% buy={t['buy_reason']}")

# 买入理由分布
print()
print('=== BUY REASON ===')
br = Counter(t['buy_reason'].split('(')[0].strip() for t in trades)
for r, n in br.most_common(10):
    print(f'{r}: {n}')

# 看波动率消退的交易，平均持仓天数（说明是不是过早卖出）
print()
print('=== 波动率消退 持仓天数 ===')
hd = []
for t in volfade:
    try:
        d1 = datetime.strptime(t['buy_date'], '%Y-%m-%d')
        d2 = datetime.strptime(t['sell_date'], '%Y-%m-%d')
        hd.append((d2 - d1).days)
    except Exception:
        pass
print(f'avg={statistics.mean(hd):.1f} median={statistics.median(hd):.1f}')
