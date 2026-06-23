#!/usr/bin/env python3
"""检查5只持仓是否满足趋势跟随加仓条件"""
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

codes = ["sh.600596", "sz.002378", "sh.600999", "sz.002137", "sz.300880"]

print("趋势跟随买入条件检查 (基于6/22数据)")
print("=" * 65)

for code in codes:
    rows = (
        sess.query(StockDaily)
        .filter(StockDaily.code == code)
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
    close = closes[i]

    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)
    vol_ma20_arr = sma(volumes, 20)

    vr = vol_ma5[i] / vol_ma20_arr[i] if vol_ma20_arr[i] > 0 else 0
    chg_5d = (close - closes[i - 5]) / closes[i - 5] * 100 if i >= 5 else 0
    chg_20d = (close - closes[i - 20]) / closes[i - 20] * 100 if i >= 20 else 0
    high_20 = max(highs[i - 19:i + 1])
    dist_high = (high_20 - close) / high_20 * 100
    dist_ma20 = (close - ma20[i]) / ma20[i] * 100
    dyn = _compute_price_volume_dynamics(closes, volumes, i)

    issues = []
    if chg_5d < 1:
        issues.append(f"5日涨幅 {chg_5d:.1f}% < 1%")
    if chg_20d < -8:
        issues.append(f"20日跌幅 {chg_20d:.1f}% < -8%")
    if close <= ma10[i] or close <= ma20[i]:
        issues.append(f"未站上MA10({ma10[i]:.2f})/MA20({ma20[i]:.2f})")
    if dist_high > 15:
        issues.append(f"距20日高点 {dist_high:.1f}% > 15%")
    if dist_ma20 > 15:
        issues.append(f"超MA20 {dist_ma20:.1f}% > 15%，追高")
    if vr < 1.3:
        issues.append(f"量比 {vr:.1f}x < 1.3x，量能不足")
    if vr > 5:
        issues.append(f"量比 {vr:.1f}x > 5x，异常爆量")
    if dyn["up_vol_ratio"] < 1.1:
        issues.append(f"涨跌量比 {dyn['up_vol_ratio']:.2f} < 1.1，量价背离")
    if dyn["consecutive_up"] < 1:
        issues.append("未连涨")

    ok = "✅ 可加仓" if not issues else "❌ 不可加"
    print(f"\n{code}  {close:.2f}  5日{chg_5d:+.1f}%  量比{vr:.1f}x  涨跌量比{dyn['up_vol_ratio']:.2f}  连涨{dyn['consecutive_up']}天  → {ok}")
    if issues:
        for x in issues:
            print(f"  [X] {x}")
    else:
        print(f"  全部条件通过")

sess.close()
