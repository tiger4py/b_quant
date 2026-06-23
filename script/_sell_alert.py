#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据持仓 + 趋势跟随卖出条件，推算次日清仓预警价/量"""
import sys, json, math
from datetime import datetime
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo
from backtest.indicators import sma
from backtest.strategy.strategy_trend_following import (
    _compute_price_volume_dynamics,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_HOLD_DAYS,
    HIGH_RETREAT_PCT, VOL_COLLAPSE_RATIO,
)

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

# 持仓
portfolio_file = ROOT_DIR / "data" / "portfolio.json"
with open(portfolio_file, encoding="utf-8") as f:
    portfolio = json.load(f)

latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
print(f"数据库最新日期: {latest_date}")
print(f"{'='*65}")

for h in portfolio["holdings"]:
    code = h["code"]
    name = h["name"]
    buy_price = h["buy_price"]
    buy_date = h["buy_date"]
    shares = h["shares"]

    # 拉K线
    rows = (
        sess.query(StockDaily)
        .filter(StockDaily.code == code)
        .order_by(StockDaily.trade_date.desc())
        .limit(45)
        .all()
    )
    rows.reverse()
    if len(rows) < 42:
        print(f"\n{code} {name}: K线不足，跳过")
        continue

    closes = [r.close for r in rows]
    highs = [r.high for r in rows]
    lows = [r.low for r in rows]
    volumes = [r.volume or 0 for r in rows]
    n = len(closes)
    i = n - 1

    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)
    vol_ma20_arr = sma(volumes, 20)

    close_today = closes[i]
    ma10_today = ma10[i]
    ma20_today = ma20[i]
    vol_ratio_today = vol_ma5[i] / vol_ma20_arr[i] if vol_ma20_arr[i] > 0 else 0

    # 持仓高点
    buy_idx = None
    for j in range(n):
        if rows[j].trade_date >= buy_date:
            buy_idx = j
            break
    if buy_idx is None:
        buy_idx = n - 5  # fallback
    peak_since_entry = max(highs[buy_idx:i+1])

    dyn = _compute_price_volume_dynamics(closes, volumes, i)

    print(f"\n{'─'*60}")
    print(f"  {code} {name}")
    print(f"  买入: {buy_date} @ {buy_price:.2f}  |  现价: {close_today:.2f}")
    print(f"  MA10: {ma10_today:.2f}  |  MA20: {ma20_today:.2f}")
    print(f"  量比(5/20): {vol_ratio_today:.2f}x  |  涨跌量比: {dyn['up_vol_ratio']:.2f}")
    print(f"  持仓高点: {peak_since_entry:.2f}  |  持仓天数: {(datetime.strptime(latest_date,'%Y-%m-%d') - datetime.strptime(buy_date,'%Y-%m-%d')).days}天")

    # ═══════ 卖出预警 ═══════
    print(f"\n  ┌─ 次日卖出预警价/量 ─────────────────────")

    # 1. 硬止损 -8%
    stop_loss_price = round(buy_price * (1 + STOP_LOSS_PCT / 100), 2)
    dist_stop = round((stop_loss_price - close_today) / close_today * 100, 1)
    print(f"  │ [止损-8%]  跌到 {stop_loss_price:.2f} 触发  (距现价 {dist_stop:+.1f}%)")

    # 2. 止盈 +25%
    take_profit_price = round(buy_price * (1 + TAKE_PROFIT_PCT / 100), 2)
    dist_tp = round((take_profit_price - close_today) / close_today * 100, 1)
    print(f"  │ [止盈+25%] 涨到 {take_profit_price:.2f} 触发  (距现价 {dist_tp:+.1f}%)")

    # 3. 趋势破坏 跌破MA20
    dist_ma20 = round((ma20_today - close_today) / close_today * 100, 1)
    print(f"  │ [趋势破坏]  跌到 MA20({ma20_today:.2f}) 以下  (距现价 {dist_ma20:+.1f}%)")

    # 4. 高位回撤 -10%
    retreat_price = round(peak_since_entry * (1 + HIGH_RETREAT_PCT / 100), 2)
    dist_retreat = round((retreat_price - close_today) / close_today * 100, 1)
    print(f"  │ [高位回撤]  跌到 {retreat_price:.2f} (距高点-10%)  (距现价 {dist_retreat:+.1f}%)")

    # 5. 跌破MA10
    dist_ma10 = round((ma10_today - close_today) / close_today * 100, 1)
    print(f"  │ [破MA10]    跌到 MA10({ma10_today:.2f}) 以下  (距现价 {dist_ma10:+.1f}%)")

    # 6. 量能崩塌
    vol_collapse_5d = round(vol_ma20_arr[i] * VOL_COLLAPSE_RATIO, 0)
    cur_vol_5d = vol_ma5[i]
    print(f"  │ [量能崩塌]  5日均量 < {vol_collapse_5d:,.0f}  (当前{cur_vol_5d:,.0f}, 需萎缩{(1-cur_vol_5d/vol_collapse_5d)*100:.0f}%)")

    # 7. 量价背离
    print(f"  │ [量价背离]  涨跌量比 < 1.1  (当前{dyn['up_vol_ratio']:.2f}, {'已触发' if dyn['up_vol_ratio'] < 1.1 else '正常'})")

    # 8. 持仓到期
    hold_days = (datetime.strptime(latest_date, "%Y-%m-%d") - datetime.strptime(buy_date, "%Y-%m-%d")).days
    days_left = MAX_HOLD_DAYS - hold_days
    print(f"  │ [持仓到期]  {MAX_HOLD_DAYS}天到期  (已持{hold_days}天, 剩余{days_left}天)")

    # ── 最危险的线 ──
    print(f"  └────────────────────────────────────────")
    threats = []
    if dist_stop > -5:
        threats.append(f"止损价 {stop_loss_price:.2f}")
    if dist_retreat > -5:
        threats.append(f"回撤价 {retreat_price:.2f}")
    if dist_ma20 > -5:
        threats.append(f"MA20 {ma20_today:.2f}")
    if dyn["up_vol_ratio"] < 1.2:
        threats.append(f"量价背离 {dyn['up_vol_ratio']:.2f}")

    if threats:
        print(f"  ⚠️ 最近危险线: {' | '.join(threats)}")
    else:
        print(f"  ✅ 各条线都在安全距离，暂无预警")

sess.close()
