import json, sys
sys.stdout.reconfigure(encoding='utf-8')

with open('data/strategy/trend_following/2026-06/2026-06-23_70.json', encoding='utf-8') as f:
    data = json.load(f)

s = data['summary']
print(f"收益={s['total_return_pct']:.2f}% 交易={s['trade_count']} 胜率={s['win_rate_pct']:.2f}% PF={s['profit_factor']}")

from collections import Counter
reasons = Counter()
for t in data['trades']:
    r = t['sell_reason']
    if '止损' in r: reasons['止损'] += 1
    elif '止盈' in r: reasons['止盈'] += 1
    elif '暴跌' in r: reasons['单日暴跌'] += 1
    elif '到期' in r: reasons['持仓到期'] += 1
    else: reasons[r] += 1

print('\n=== 卖出原因 ===')
for k,v in reasons.most_common():
    trades = [t for t in data['trades'] if k in t['sell_reason']]
    profits = [t['profit_pct'] for t in trades]
    wins = sum(1 for p in profits if p > 0)
    print(f'  {k}: {v}笔 ({v/s["trade_count"]*100:.1f}%), 胜率{wins/len(trades)*100:.1f}%, 平均盈亏{sum(profits)/len(profits):.1f}%, 累计{sum(profits):.1f}%')

print(f'\n=== v2 vs v4 对比 ===')
print(f'  v2: 387笔, 止盈25(6.5%), 到期287(74.2%), 止损43(11.1%), 暴跌27(7.0%)')
print(f'  v4: {s["trade_count"]}笔, 止盈{reasons.get("止盈",0)}({reasons.get("止盈",0)/s["trade_count"]*100:.1f}%), 到期{reasons.get("持仓到期",0)}({reasons.get("持仓到期",0)/s["trade_count"]*100:.1f}%), 止损{reasons.get("止损",0)}({reasons.get("止损",0)/s["trade_count"]*100:.1f}%), 暴跌{reasons.get("单日暴跌",0)}({reasons.get("单日暴跌",0)/s["trade_count"]*100:.1f}%)')
