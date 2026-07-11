"""交易数据分析 + 大盘环境计算 + 持仓管理"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / 'data' / 'day_stock' / '_tmp' / 'user_trades.csv'
TRADE_LOG = ROOT / 'data' / 'trade_log.json'
PORTFOLIO = ROOT / 'data' / 'portfolio.json'
MARKET_CTX = ROOT / 'data' / 'market_context.json'


def code_prefix(code):
    """根据代码推断交易所前缀"""
    code = str(code)
    if code.startswith(('300', '301', '000', '001', '002', '003')):
        return f'sz.{code}'
    elif code.startswith(('600', '601', '603', '605', '688', '563', '510', '511')):
        return f'sh.{code}'
    elif code.startswith(('920', '830', '831', '832', '833', '834', '835', '836', '837', '838', '839')):
        return f'bj.{code}'
    elif code.startswith('4'):
        return f'bj.{code}'
    else:
        return code


def load_csv():
    rows = []
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for r in reader:
            code = r['证券代码'].strip()
            name = r['证券名称'].strip()
            action = r['操作类型'].strip()
            if not code or action == '赎回':
                continue  # skip 货币基金赎回等
            if '.HK' in code:
                continue  # skip 港股
            try:
                rows.append({
                    'date': r['日期'].strip(),
                    'code': code,
                    'name': name,
                    'action': 'buy' if action == '买入' else 'sell',
                    'price': float(r['成交价']),
                    'shares': int(r['成交量']),
                    'amount': abs(float(r['发生金额'])),
                    'fee': float(r['交易费用']),
                })
            except (ValueError, KeyError):
                continue
    rows.sort(key=lambda r: (r['date'], r['code']))
    return rows


def build_trade_log(rows):
    """按时间顺序重建交易日志"""
    log = []
    for i, r in enumerate(rows):
        entry = {
            'id': i + 1,
            'date': r['date'],
            'code': code_prefix(r['code']),
            'name': r['name'],
            'action': r['action'],
            'price': r['price'],
            'shares': r['shares'],
            'amount': r['amount'],
            'reason': '',
        }
        log.append(entry)
    return log


def build_portfolio(rows):
    """FIFO 计算最终持仓"""
    # 按股票分组
    stocks = defaultdict(list)
    for r in rows:
        stocks[r['code']].append(r)

    holdings = []
    for code, trades in stocks.items():
        buys = []
        for t in trades:
            if t['action'] == 'buy':
                buys.append(t)
            elif t['action'] == 'sell':
                ss = t['shares']
                while ss > 0 and buys:
                    b = buys[0]
                    m = min(ss, b['shares'])
                    ss -= m
                    if m >= b['shares']:
                        buys.pop(0)
                    else:
                        b['shares'] -= m

        if buys:
            total_shares = sum(b['shares'] for b in buys)
            total_cost = sum(b['price'] * b['shares'] for b in buys)
            avg_price = total_cost / total_shares if total_shares else 0
            earliest_date = min(b['date'] for b in buys)
            holdings.append({
                'code': code_prefix(code),
                'name': trades[0]['name'],
                'shares': total_shares,
                'buy_price': round(avg_price, 3),
                'buy_date': earliest_date,
            })

    # 估算现金：假设初始资金 100万，计算剩余
    total_bought = sum(
        abs(r['amount']) for r in rows if r['action'] == 'buy'
    )
    total_sold = sum(
        r['amount'] for r in rows if r['action'] == 'sell'
    )
    # 现金 = 初始资金 - 净投入
    estimated_cash = 1_000_000 - (total_bought - total_sold)

    return {
        'cash': round(estimated_cash, 2),
        'max_positions': 6,
        'holdings': holdings,
    }


def main():
    rows = load_csv()
    print(f'读取 {len(rows)} 条 A 股交易记录')

    # 1. 生成 trade_log.json
    log = build_trade_log(rows)
    with open(TRADE_LOG, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print(f'trade_log.json: {len(log)} 条记录')

    # 2. 生成 portfolio.json
    portfolio = build_portfolio(rows)
    with open(PORTFOLIO, 'w', encoding='utf-8') as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)
    print(f'portfolio.json: {len(portfolio["holdings"])} 个持仓, 现金 {portfolio["cash"]:,.0f}')

    # 3. 打印持仓
    print()
    print('当前持仓:')
    for h in portfolio['holdings']:
        mv = h['shares'] * h['buy_price']
        print(f'  {h["code"]} {h["name"]}: {h["shares"]}股 均价{h["buy_price"]:.2f} 成本{mv:,.0f}')


if __name__ == '__main__':
    main()
