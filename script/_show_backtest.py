import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DATABASE_URL
from models.stock import BacktestCache

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()
row = sess.get(BacktestCache, 'volatility_breakout_market_1000_pos5')
if row:
    r = json.loads(row.result_json)
    s = r['summary']
    sel = r['selection']
    print('=== 波动率V反策略 回测结果 ===')
    print(f'数据区间: {sel["latest_trade_date"]} 前推 {sel["days"]} 天')
    print(f'股票数: {sel["stock_count"]} | 初始资金: {sel["initial_cash"]:,.0f} | 最大持仓: {sel["max_positions"]}')
    print()
    print(f'最终权益: {s["final_equity"]:,.0f}')
    print(f'总收益率: {s["total_return_pct"]:.2f}%')
    print(f'买入持有: {s.get("buy_hold_return_pct", "N/A")}')
    print(f'最大回撤: {s["max_drawdown_pct"]:.2f}%')
    print(f'交易笔数: {s["trade_count"]} | 胜率: {s["win_rate_pct"]:.2f}%')
    print(f'平均盈亏: {s["avg_profit_pct"]:.2f}% | 盈亏比: {s["profit_factor"]}')
    print()
    print('TOP15 盈利股票:')
    stocks = sorted(r.get('stock_summaries', []), key=lambda x: x['profit'], reverse=True)[:15]
    for i, x in enumerate(stocks, 1):
        print(f'  {i:2}. {x["code"]} {x["name"]:<8} | 盈利: {x["profit"]:>12,.0f} | 交易: {x["trade_count"]:>3}笔 | 胜率: {x["win_rate_pct"]:.1f}%')

    trades = sorted(r.get('trades', []), key=lambda x: x['buy_date'], reverse=True)[:10]
    print()
    print('最近10笔交易:')
    for t in trades:
        print(f'  {t["buy_date"]} -> {t["sell_date"]} | {t["code"]} {t["name"]} | {t["profit_pct"]:+.2f}% | {t["buy_reason"][:80]}')

    # Market gate summary
    gate = r.get('market_gate', {})
    print()
    print(f'大盘门控: 允许 {gate.get("allowed_days", "?")}天 / 禁止 {gate.get("blocked_days", "?")}天')
else:
    print('No cache found')
sess.close()
