import json
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import BacktestCache, StockDaily, StockInfo
from collections import defaultdict

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

row = sess.get(BacktestCache, 'volatility_breakout_market_1000_pos5')
result = json.loads(row.result_json)
trades = result.get('trades', [])

# Compute volatility metrics for recently bought stocks
def compute_vol_metrics(bars):
    """Compute vol_5d, vol_60d and ratio for a stock."""
    if len(bars) < 65:
        return None
    closes = [float(b.close) for b in bars]
    daily_vol = [0.0]
    for i in range(1, len(closes)):
        chg = abs(closes[i]/closes[i-1] - 1)
        daily_vol.append(chg)

    # sma
    def sma(values, window):
        result = []
        for i in range(len(values)):
            if i + 1 < window:
                result.append(None)
            else:
                result.append(sum(values[i+1-window:i+1])/window)
        return result

    v5 = sma(daily_vol, 5)
    v60 = sma(daily_vol, 60)

    return {
        'v5': v5,
        'v60': v60,
        'daily_vol': daily_vol,
        'closes': closes,
    }

# Get recent 10 buy dates
from collections import Counter
buy_dates = sorted(set(t['buy_date'] for t in trades), reverse=True)[:10]

print("=" * 80)
print("波动率V反策略 - 最近10个交易日选股明细")
print("=" * 80)

for date in buy_dates:
    day_trades = [t for t in trades if t['buy_date'] == date]
    print(f"\n{'='*60}")
    print(f"日期: {date} | 买入 {len(day_trades)} 只")
    print(f"{'='*60}")

    for t in day_trades:
        code = t['code']
        name = t['name']

        # Get stock bars for volatility calculation
        bars = sess.query(StockDaily).filter(
            StockDaily.code == code,
            StockDaily.trade_date <= '2026-06-03'
        ).order_by(StockDaily.trade_date.desc()).limit(200).all()

        if len(bars) < 65:
            print(f"  {code} {name}: 数据不足")
            continue

        bars = list(reversed(bars))
        metrics = compute_vol_metrics(bars)
        if metrics is None:
            print(f"  {code} {name}: 计算失败")
            continue

        idx = -1
        v5_now = metrics['v5'][idx] or 0
        v60_now = metrics['v60'][idx] or 0
        ratio = v5_now / v60_now if v60_now > 0.001 else 0

        # Last 10 days daily vol
        last10_vol = metrics['daily_vol'][-10:]
        last10_changes = [(metrics['closes'][i]/metrics['closes'][i-1]-1)*100 for i in range(len(metrics['closes'])-10, len(metrics['closes']))]

        profit_str = f"{t['profit_pct']:+.2f}%" if t['sell_date'] > date else "持仓中"
        sell_info = f"-> {t['sell_date']} {profit_str}" if t['sell_date'] > date else f"持仓中 (买入于{date})"

        print(f"\n  [{code}] {name}")
        print(f"    交易: {t['buy_date']} -> {t['sell_date']} | 盈亏: {profit_str}")
        print(f"    买入信号: {t['buy_reason']}")
        if t['sell_date'] > date:
            print(f"    卖出信号: {t['sell_reason']}")

        print(f"    波动率指标 (最新):")
        print(f"      vol_5d={v5_now*100:.2f}%  vol_60d={v60_now*100:.2f}%  ratio={ratio:.2f}x")
        print(f"    近10日波动率(%): {'  '.join(f'{v*100:.2f}' for v in last10_vol)}")
        print(f"    近10日涨跌幅(%): {'  '.join(f'{c:+.2f}' for c in last10_changes)}")

# Also show market gate context
print(f"\n{'='*80}")
print("最近10天大盘环境 (market_gate)")
print(f"{'='*80}")
gate = result.get('market_gate', {})
for g in gate.get('recent', [])[-10:]:
    status = "ALLOW" if g.get('allowed') else "BLOCK"
    reasons = " | ".join(g.get('reasons', []))
    print(f"  {g['date']}  [{status}]  {reasons}")

sess.close()
print("\nDone.")
