#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sz.002378 章源钨业 — 含预估6/23数据的趋势跟随诊断"""
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

rows = (
    sess.query(StockDaily)
    .filter(StockDaily.code == "sz.002378")
    .order_by(StockDaily.trade_date.desc())
    .limit(39)
    .all()
)
rows.reverse()

closes = [r.close for r in rows]
volumes = [r.volume or 0 for r in rows]

# ---- 追加预估的 6/23 ----
EST_CLOSE = round(40.52 * (1 - 0.075), 2)   # 跌7.5%
EST_VOL = 130_000_000                         # 1.3亿

closes.append(EST_CLOSE)
volumes.append(EST_VOL)

n = len(closes)
i = n - 1

ma10 = sma(closes, 10)
ma20 = sma(closes, 20)
vol_ma5 = sma(volumes, 5)
vol_ma20_arr = sma(volumes, 20)
vr = vol_ma5[i] / vol_ma20_arr[i] if vol_ma20_arr[i] > 0 else 0
dyn = _compute_price_volume_dynamics(closes, volumes, i)

# 20日高点
high_20_hist = max(r.high for r in rows[-20:])
high_20_all = max(high_20_hist, EST_CLOSE)
retreat = (high_20_all - EST_CLOSE) / high_20_all * 100

print("=" * 55)
print("  sz.002378 章源钨业 — 含预估6/23 完整诊断")
print("=" * 55)
print()
print(f"  6/23 预估: 收盘 {EST_CLOSE} ({-7.5:.1f}%)  |  成交量 1.3亿")
print()
print(f"  收盘:  {EST_CLOSE}")
print(f"  MA10:  {ma10[i]:.2f}")
print(f"  MA20:  {ma20[i]:.2f}")
print(f"  量比(5/20均量): {vr:.2f}x")
print(f"  涨跌量比:       {dyn['up_vol_ratio']:.2f}")
print(f"  连涨天数:       {dyn['consecutive_up']}天 → 今日跌即归零")
print(f"  近10天:         涨{dyn['up_days']}天 / 跌{dyn['down_days']}天")
print(f"  20日高点:       {high_20_all:.2f}")
print(f"  高位回撤:       {retreat:.1f}%")
print()

# ======== 卖出条件 ========
print("=" * 55)
print("  卖出条件逐项检查")
print("=" * 55)

issues = []

# 1. 趋势破坏
cond1 = EST_CLOSE <= ma20[i]
tag1 = "[X]" if cond1 else "[OK]"
print(f"  {tag1} 趋势破坏(破MA20):    {EST_CLOSE} vs MA20({ma20[i]:.2f})")
if cond1:
    issues.append("趋势破坏: 收盘跌破MA20")

# 2. 量能崩塌
cond2 = vr < 0.7
tag2 = "[X]" if cond2 else "[OK]"
print(f"  {tag2} 量能崩塌(<0.7x):     量比 {vr:.2f}x")
if cond2:
    issues.append(f"量能崩塌: 量比{vr:.2f}<0.7")

# 3. 量价背离
cond3 = dyn["up_vol_ratio"] < 1.1
tag3 = "[X]" if cond3 else "[OK]"
print(f"  {tag3} 量价背离(<1.1):      涨跌量比 {dyn['up_vol_ratio']:.2f}")
if cond3:
    issues.append(f"量价背离: 涨跌量比{dyn['up_vol_ratio']:.2f}<1.1")

# 4. 跌破MA10
cond4 = EST_CLOSE <= ma10[i]
tag4 = "[!]" if cond4 else "[OK]"
print(f"  {tag4} 跌破MA10:           {EST_CLOSE} vs MA10({ma10[i]:.2f})")
if cond4:
    issues.append("跌破MA10: 短期趋势转弱")

# 5. 高位回撤
cond5 = retreat > 10
tag5 = "[X]" if cond5 else "[OK]"
print(f"  {tag5} 高位回撤(>10%):     {retreat:.1f}%")
if cond5:
    issues.append(f"高位回撤: {retreat:.1f}%>10%")

# 6. 硬止损 — 需要买入价
print(f"  [--] 硬止损(-8%):         需要买入价才能判断")

# 当日跌幅
print()
print(f"  [!] 单日跌 {7.5:.1f}% → 已逼近硬止损线(-8%)，仅差0.5%")

print()
print("=" * 55)
if issues:
    print(f"  结论: 触发 {len(issues)} 条卖出 → 必须清仓")
    for x in issues:
        print(f"    - {x}")
else:
    print(f"  结论: 未触发自动卖出条件")

sess.close()
