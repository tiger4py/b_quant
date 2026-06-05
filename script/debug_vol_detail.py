import json
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import BacktestCache

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

row = sess.get(BacktestCache, 'volatility_breakout_market_1000_pos5')
if not row:
    print('No cache found')
    sess.close()
    exit()

result = json.loads(row.result_json)

# Recent market gate
gate = result.get('market_gate', {})
recent_gates = gate.get('recent', [])[-10:]
print('=== 最近10天大盘环境 ===')
for g in recent_gates:
    status = '允许' if g.get('allowed') else '禁止'
    reasons = ', '.join(g.get('reasons', []))
    print(f"  {g['date']} | {status} | {reasons}")

# Latest trades
trades = result.get('trades', [])
trades_sorted = sorted(trades, key=lambda x: x['buy_date'], reverse=True)
print('\n=== 最近买入的30笔交易 ===')
for t in trades_sorted[:30]:
    print(f"  {t['buy_date']} -> {t['sell_date']} | {t['code']} {t['name']} | {t['profit_pct']:+.2f}% | 买:{t['buy_reason'][:60]} | 卖:{t['sell_reason']}")

print(f'\n总交易数: {len(trades)}')

# Latest buy dates (最近10个交易日买入的)
print('\n=== 最近10个交易日买入详情 ===')
from collections import Counter
buy_date_counts = Counter(t['buy_date'] for t in trades)
latest_buy_dates = sorted(buy_date_counts.keys(), reverse=True)[:10]
for date in latest_buy_dates:
    day_trades = [t for t in trades if t['buy_date'] == date]
    print(f"\n{date} (买入{len(day_trades)}只):")
    for t in day_trades[:10]:
        print(f"  {t['code']} {t['name']} | {t['profit_pct']:+.2f}% | {t['buy_reason'][:80]}")

sess.close()
