#!/usr/bin/env python3
"""实益达 近10天成交量明细"""
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily
from backtest.strategy.strategy_trend_following import _compute_price_volume_dynamics

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

rows = sess.query(StockDaily).filter(StockDaily.code == "sz.002137")\
    .order_by(StockDaily.trade_date.desc()).limit(12).all()
rows.reverse()

print("实益达(sz.002137) 近10天量价明细")
print(f"{'日期':<12} {'开盘':>7} {'收盘':>7} {'涨跌%':>8} {'成交量(万)':>12} {'成交额(万)':>12}")
for r in rows[-10:]:
    chg = (r.close - r.open) / r.open * 100
    vol_w = r.volume / 10000
    amt_w = r.amount / 10000 if r.amount else 0
    print(f"{r.trade_date:<12} {r.open:>7.2f} {r.close:>7.2f} {chg:>8.2f} {vol_w:>12.0f} {amt_w:>12.0f}")

# 逐日涨跌和量
closes = [r.close for r in rows]
volumes = [r.volume or 0 for r in rows]

print()
print("逐日方向 + 成交量:")
up_vols, down_vols = [], []
total = len(rows)
for j in range(total - 10, total):
    chg = closes[j] - closes[j - 1]
    vol_w = volumes[j] / 10000
    if chg > 0:
        tag = "[涨]"
        up_vols.append(volumes[j])
    elif chg < 0:
        tag = "[跌]"
        down_vols.append(volumes[j])
    else:
        tag = "[平]"
    bar = "#" * int(vol_w / 20000)
    print(f"  {rows[j].trade_date}  {tag}  {vol_w:>8,.0f}万  {bar}")

print()
up_avg = sum(up_vols) / len(up_vols) / 10000 if up_vols else 0
down_avg = sum(down_vols) / len(down_vols) / 10000 if down_vols else 0
dyn = _compute_price_volume_dynamics(closes, volumes, total - 1)
print(f"上涨日({len(up_vols)}天) 日均量: {up_avg:,.0f}万")
print(f"下跌日({len(down_vols)}天) 日均量: {down_avg:,.0f}万")
print(f"涨跌量比 = {up_avg:,.0f} / {down_avg:,.0f} = {dyn['up_vol_ratio']:.2f}")
print(f"策略要求 >= 1.1  →  {'[X] 背离，触发卖出' if dyn['up_vol_ratio'] < 1.1 else '[OK] 正常'}")

sess.close()
