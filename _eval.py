# -*- coding: utf-8 -*-
"""单只股票 Alpha #042 评估"""
import sys; sys.path.insert(0, '.')
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily
from backtest.strategy.strategy_alpha042 import (
    _rolling_corr, _rolling_max, _has_recent_limit_up,
    CORR_WINDOW, CORR_BUY_MAX, CORR_SELL_THRESH,
    VOL_SHORT, VOL_LONG, VOL_AMP_MIN, VOL_AMP_MAX,
    PRICE_NEAR_HIGH_LOOKBACK, PRICE_NEAR_HIGH_PCT,
    CHG_5D_MIN, LIMIT_UP_PCT, LIMIT_UP_LOOKBACK, MIN_AMOUNT,
    MAX_HOLD_DAYS,
)
from backtest.indicators import sma

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)

with Session() as s:
    bars = s.query(StockDaily).filter(
        StockDaily.code == 'sz.300285'
    ).order_by(StockDaily.trade_date.asc()).all()

closes = [b.close for b in bars]
highs = [b.high for b in bars]
volumes = [b.volume or 0 for b in bars]
amounts = [b.amount or 0 for b in bars]
dates = [b.trade_date for b in bars]
n = len(closes)
print(f'国瓷材料 sz.300285 共 {n} 根K线，{dates[0]} ~ {dates[-1]}')

# 日涨跌幅
daily_change = [0.0]
daily_vol = [0.0]
for i in range(1, n):
    chg = closes[i] / closes[i - 1] - 1 if closes[i - 1] else 0
    daily_change.append(chg)
    daily_vol.append(abs(chg))

vol_short = sma(daily_vol, VOL_SHORT)
vol_long = sma(daily_vol, VOL_LONG)
high_vol_corr = _rolling_corr(highs, volumes, CORR_WINDOW)
high_20 = _rolling_max(highs, PRICE_NEAR_HIGH_LOOKBACK)

min_idx = max(VOL_LONG, PRICE_NEAR_HIGH_LOOKBACK, CORR_WINDOW) + 5

# 评估最近30天
print(f'\n{"="*70}')
print(f'Alpha #042 买入条件逐项检查（最近30个交易日）')
print(f'{"="*70}')
print(f'{"日期":<12} {"close":>7} {"corr":>6} {"vol_amp":>7} {"距20高%":>8} {"5日%":>7} {"量":>9} 满足?')
print('-' * 70)

last_n = 30
for i in range(n - last_n, n):
    close = closes[i]
    date = dates[i]
    corr_val = high_vol_corr[i]
    high_20_i = high_20[i]
    amount = amounts[i]

    if corr_val is None or high_20_i is None:
        continue

    # vol_amp 即时算
    vs = vol_short[i]
    vl = vol_long[i]
    if vs is None or vl is None or vl < 0.0001:
        continue
    vol_amp = vs / vl

    # chg_5d
    if i < 5 or closes[i - 5] <= 0:
        continue
    chg_5d = (close - closes[i - 5]) / closes[i - 5]

    # 6 个买入条件
    c1 = corr_val < CORR_BUY_MAX
    c2 = VOL_AMP_MIN <= vol_amp <= VOL_AMP_MAX
    c3 = close >= high_20_i * (1 - PRICE_NEAR_HIGH_PCT)
    c4 = chg_5d > CHG_5D_MIN
    c5 = not _has_recent_limit_up(daily_change, i)
    c6 = amount >= MIN_AMOUNT
    all_ok = c1 and c2 and c3 and c4 and c5 and c6

    flags = ''
    flags += '1' if c1 else '.'
    flags += '2' if c2 else '.'
    flags += '3' if c3 else '.'
    flags += '4' if c4 else '.'
    flags += '5' if c5 else '.'
    flags += '6' if c6 else '.'

    marker = ' ★★ 买入' if all_ok else ''
    dist_high = (close / high_20_i - 1) * 100

    print(f'{date:<12} {close:>7.2f} {corr_val:>6.2f} {vol_amp:>7.2f} {dist_high:>7.1f}% {chg_5d*100:>6.1f}% {amount:>9,.0f} [{flags}]{marker}')

# 总结最新一天
print(f'\n{"="*70}')
print(f'最新交易日: {dates[-1]}')
i = n - 1
close = closes[i]
corr_val = high_vol_corr[i]
high_20_i = high_20[i]
vs = vol_short[i]; vl = vol_long[i]
vol_amp = vs / vl if vs and vl and vl > 0.0001 else None
chg_5d = (close - closes[i - 5]) / closes[i - 5] if i >= 5 and closes[i - 5] > 0 else None

print(f'  收盘: {close:.2f}')
print(f'  corr(high,vol,{CORR_WINDOW}): {corr_val:.3f} (需 < {CORR_BUY_MAX}) → {"✅" if corr_val is not None and corr_val < CORR_BUY_MAX else "❌"}')
print(f'  vol_amp: {vol_amp:.2f}x (需 {VOL_AMP_MIN}-{VOL_AMP_MAX}) → {"✅" if vol_amp and VOL_AMP_MIN <= vol_amp <= VOL_AMP_MAX else "❌"}')
print(f'  距20日高: {(close/high_20_i-1)*100:.1f}% (需 ≥ -{PRICE_NEAR_HIGH_PCT*100:.0f}%) → {"✅" if high_20_i and close >= high_20_i * (1 - PRICE_NEAR_HIGH_PCT) else "❌"}')
print(f'  5日涨幅: {chg_5d*100:.1f}% (需 > {CHG_5D_MIN*100:.0f}%) → {"✅" if chg_5d is not None and chg_5d > CHG_5D_MIN else "❌"}')
print(f'  近{LIMIT_UP_LOOKBACK}日涨停: {"有" if _has_recent_limit_up(daily_change, i) else "无"} (需无) → {"✅" if not _has_recent_limit_up(daily_change, i) else "❌"}')
print(f'  成交额: {amounts[i]:,.0f} (需 ≥ {MIN_AMOUNT:,}) → {"✅" if amounts[i] >= MIN_AMOUNT else "❌"}')

all_ok = (
    corr_val is not None and corr_val < CORR_BUY_MAX and
    vol_amp and VOL_AMP_MIN <= vol_amp <= VOL_AMP_MAX and
    high_20_i and close >= high_20_i * (1 - PRICE_NEAR_HIGH_PCT) and
    chg_5d is not None and chg_5d > CHG_5D_MIN and
    not _has_recent_limit_up(daily_change, i) and
    amounts[i] >= MIN_AMOUNT
)
print(f'\n  → Alpha #042 买入信号: {"✅ 全满足，可以买" if all_ok else "❌ 条件不满足，不能买"}')
