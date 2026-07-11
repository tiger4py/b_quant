"""给 trade_log.json 中每条卖出记录计算 FIFO 盈亏（缺失的补0）"""
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRADE_LOG = ROOT / 'data' / 'trade_log.json'


def main():
    with open(TRADE_LOG, 'r', encoding='utf-8') as f:
        logs = json.load(f)

    # 按股票分组，按时间排序计算 FIFO
    stocks = defaultdict(list)
    for l in logs:
        stocks[l['code']].append(l)

    for code, entries in stocks.items():
        entries.sort(key=lambda e: (e['date'], e['id']))
        buy_stack = []  # [(price, shares, id)]

        for e in entries:
            if e['action'] == 'buy':
                buy_stack.append([e['price'], e['shares'], e['id']])
            elif e['action'] == 'sell':
                ss = e['shares']
                sp = e['price']
                total_pnl = 0.0
                matched_shares = 0
                matched_buy_prices = []

                while ss > 0 and buy_stack:
                    bp, bs, bid = buy_stack[0]
                    m = min(ss, bs)
                    pnl = (sp - bp) * m
                    total_pnl += pnl
                    matched_shares += m
                    matched_buy_prices.append((bp, m))
                    ss -= m
                    if m >= bs:
                        buy_stack.pop(0)
                    else:
                        buy_stack[0] = (bp, bs - m, bid)

                if matched_shares > 0:
                    avg_buy = sum(p * s for p, s in matched_buy_prices) / matched_shares
                    e['pnl'] = round(total_pnl, 2)
                    e['pnlPct'] = round((sp / avg_buy - 1) * 100, 2)
                    e['buyPrice'] = round(avg_buy, 3)
                    e['amount'] = round(sp * e['shares'], 2)
                else:
                    # 没有匹配的买入记录（数据范围之外的持仓），设为0
                    e['pnl'] = 0
                    e['pnlPct'] = 0
                    e['buyPrice'] = sp  # 用卖出价兜底

    # Save
    with open(TRADE_LOG, 'w', encoding='utf-8') as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

    # Stats
    sells = [l for l in logs if l['action'] == 'sell']
    total_pnl = sum(l.get('pnl', 0) for l in sells)
    wins = sum(1 for l in sells if l.get('pnl', 0) > 0)
    missing = sum(1 for l in sells if 'pnl' not in l)
    print(f'Sells: {len(sells)}, with PnL: {len(sells) - missing}, missing: {missing}')
    print(f'Total realized PnL: {total_pnl:+,.2f}')
    print(f'Wins: {wins}/{len(sells)} ({wins/len(sells)*100:.0f}%)')


if __name__ == '__main__':
    main()
