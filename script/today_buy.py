"""
今日买入筛选脚本 - 波动率V反策略
1. 检查最新数据日期
2. 跑策略找今天的买入候选
3. 输出结果
"""
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, func, desc
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo
from backtest.registry import get_strategy
from collections import defaultdict

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

# 1. 检查最新日期
latest = sess.query(func.max(StockDaily.trade_date)).scalar()
cnt = sess.query(func.count(StockDaily.id)).filter(StockDaily.trade_date == latest).scalar()
print(f"数据库最新日期: {latest}, 数据条数: {cnt}")

# 2. 获取所有活跃股票的最新K线
latest_rows = (
    sess.query(StockInfo, StockDaily)
    .join(StockDaily, StockInfo.code == StockDaily.code)
    .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date == latest)
    .all()
)
stock_map = {stock.code: {"code": stock.code, "name": stock.name, "market": stock.market} for stock, daily in latest_rows}
print(f"活跃股票数: {len(stock_map)}")

# 3. 取最近 200 天的所有K线
date_rows = (
    sess.query(StockDaily.trade_date)
    .distinct()
    .order_by(desc(StockDaily.trade_date))
    .limit(200)
    .all()
)
cutoff = min(row[0] for row in date_rows)

rows = (
    sess.query(StockDaily)
    .join(StockInfo, StockDaily.code == StockInfo.code)
    .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date >= cutoff)
    .order_by(StockDaily.code, StockDaily.trade_date)
    .all()
)

bars_by_code = defaultdict(list)
for row in rows:
    bars_by_code[row.code].append({
        "trade_date": row.trade_date,
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "volume": row.volume,
        "amount": row.amount,
    })

print(f"有效股票(>=65天K线): ", end="")

# 4. 对每只股票跑策略，只看最新一天
strategy = get_strategy("volatility_breakout")

# 构建 market_stats
from backtest.portfolio import _build_market_stats
market_stats = _build_market_stats(bars_by_code)

buy_candidates = []
valid_count = 0
for code, bars in bars_by_code.items():
    if len(bars) < 65:
        continue
    valid_count += 1

    signals = strategy.generate_signals(bars)
    # 只看最近一天的买入信号
    for s in signals:
        if s["date"] == latest and s["action"] == "buy":
            stock = stock_map.get(code, {"name": code})
            buy_candidates.append({
                "code": code,
                "name": stock["name"],
                "reason": s["reason"],
            })

print(valid_count)

# 5. 检查 market_gate
gate_result = None
if hasattr(strategy, "market_gate"):
    gate_result = strategy.market_gate(latest, market_stats)
    print(f"\n大盘环境 [{latest}]:")
    print(f"  允许开仓: {gate_result['allowed']}")
    print(f"  原因: {' | '.join(gate_result['reasons'])}")

# 6. 输出候选
print(f"\n{'='*80}")
print(f"今日买入候选 ({latest}): {len(buy_candidates)} 只")
print(f"{'='*80}")

if not buy_candidates:
    print("(无符合条件的买入信号)")
else:
    for i, c in enumerate(buy_candidates, 1):
        print(f"\n  [{i}] {c['code']} {c['name']}")
        print(f"      信号: {c['reason']}")

sess.close()
print("\nDone.")
