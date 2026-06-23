#!/usr/bin/env python3
"""对比 新安/章源 买入时 vs 现在的指标变化"""
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
from backtest.indicators import sma
from backtest.strategy.strategy_trend_following import _compute_price_volume_dynamics

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

checks = [
    ("sh.600596", "新安股份", "2026-06-08", 13.58),
    ("sz.002378", "章源钨业", "2026-06-12", 34.64),
]

for code, name, buy_date, buy_price in checks:
    # 拉K线到买入日为止
    rows = (
        sess.query(StockDaily)
        .filter(StockDaily.code == code, StockDaily.trade_date <= buy_date)
        .order_by(StockDaily.trade_date.desc())
        .limit(42)
        .all()
    )
    rows.reverse()
    closes = [r.close for r in rows]
    volumes = [r.volume or 0 for r in rows]
    highs = [r.high for r in rows]
    n = len(closes)
    i = n - 1

    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)
    vol_ma20_arr = sma(volumes, 20)
    vr = vol_ma5[i] / vol_ma20_arr[i] if vol_ma20_arr[i] > 0 else 0
    chg_5d = (closes[i] - closes[i - 5]) / closes[i - 5] * 100 if i >= 5 else 0
    chg_20d = (closes[i] - closes[i - 20]) / closes[i - 20] * 100 if i >= 20 else 0
    high_20 = max(highs[i - 19:i + 1])
    dist_ma20 = (closes[i] - ma20[i]) / ma20[i] * 100
    dyn = _compute_price_volume_dynamics(closes, volumes, i)

    # 现在拉最新数据
    rows_now = (
        sess.query(StockDaily)
        .filter(StockDaily.code == code)
        .order_by(StockDaily.trade_date.desc())
        .limit(42)
        .all()
    )
    rows_now.reverse()
    closes_n = [r.close for r in rows_now]
    volumes_n = [r.volume or 0 for r in rows_now]
    highs_n = [r.high for r in rows_now]
    nn = len(closes_n)
    ii = nn - 1
    ma10_n = sma(closes_n, 10)
    ma20_n = sma(closes_n, 20)
    vol_ma5_n = sma(volumes_n, 5)
    vol_ma20_n = sma(volumes_n, 20)
    vr_n = vol_ma5_n[ii] / vol_ma20_n[ii] if vol_ma20_n[ii] > 0 else 0
    chg_5d_n = (closes_n[ii] - closes_n[ii - 5]) / closes_n[ii - 5] * 100 if ii >= 5 else 0
    dist_ma20_n = (closes_n[ii] - ma20_n[ii]) / ma20_n[ii] * 100
    dyn_n = _compute_price_volume_dynamics(closes_n, volumes_n, ii)

    print("=" * 55)
    print(f"  {code} {name}")
    print(f"  策略买入日: {buy_date} @ {buy_price:.2f}")
    print(f"{'':>6} {'买入时':>10} {'现在(6/22)':>12}  {'变化':>10}")
    print(f"  {'收盘':>4} {closes[i]:>10.2f} {closes_n[ii]:>12.2f}  {(closes_n[ii]/closes[i]-1)*100:>+.1f}%")
    print(f"  {'MA20':>4} {ma20[i]:>10.2f} {ma20_n[ii]:>12.2f}")
    print(f"  {'超MA20':>4} {dist_ma20:>9.1f}% {dist_ma20_n:>11.1f}%")
    print(f"  {'5日涨':>4} {chg_5d:>9.1f}% {chg_5d_n:>11.1f}%")
    print(f"  {'量比':>4} {vr:>9.1f}x {vr_n:>11.1f}x")
    print(f"  {'涨跌量比':>4} {dyn['up_vol_ratio']:>9.2f} {dyn_n['up_vol_ratio']:>11.2f}")
    print(f"  {'连涨':>4} {dyn['consecutive_up']:>9}天 {dyn_n['consecutive_up']:>10}天")

    # 判断
    old_ok = dyn["up_vol_ratio"] >= 1.1 and vr >= 1.3
    new_ok = dyn_n["up_vol_ratio"] >= 1.1 and vr_n >= 1.3
    print(f"\n  买入时: {'✅ 条件满足' if old_ok else '❌'}")
    print(f"  现在:   {'✅ 条件满足' if new_ok else '❌ 涨跌量比崩塌，策略已不认'}")

sess.close()
