#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全市场4900只趋势跟随扫描，写入 trade_history.json"""
import sys, json, time
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
    generate_signals, _compute_price_volume_dynamics,
    PRICE_UP_5D_MIN, PRICE_UP_20D_MIN, PRICE_DOWN_40D_MAX,
    VOL_RATIO_BUY, VOL_RATIO_MAX, UP_VOL_RATIO, MIN_CONSEC_UP,
    CLOSE_ABOVE_MA20, PRICE_NEAR_20D_HIGH, PRICE_ABOVE_MA20_MAX,
)

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

latest = sess.query(func.max(StockDaily.trade_date)).scalar()
print(f"数据库最新日期: {latest}")

# 所有正常股票
all_codes = [
    r[0] for r in sess.query(StockInfo.code)
    .filter(StockInfo.type == "1", StockInfo.status == 1)
    .order_by(StockInfo.code).all()
]
total = len(all_codes)
print(f"全市场: {total} 只")
print()

# 批量加载K线（一次性加载所有stock_daily，分组）
from collections import defaultdict
print("加载全市场K线...")
cutoff_date = sess.query(StockDaily.trade_date)\
    .distinct().order_by(StockDaily.trade_date.desc())\
    .offset(60).limit(1).scalar()
print(f"cutoff_date: {cutoff_date}")

rows = sess.query(StockDaily).filter(
    StockDaily.trade_date >= cutoff_date,
    StockDaily.code.in_(all_codes)
).order_by(StockDaily.code, StockDaily.trade_date).all()

bars_by_code = defaultdict(list)
for r in rows:
    bars_by_code[r.code].append({
        "trade_date": r.trade_date, "open": r.open, "high": r.high,
        "low": r.low, "close": r.close, "volume": r.volume, "amount": r.amount,
    })

print(f"有K线股票: {len(bars_by_code)} 只")

# 扫描
buy_signals = []
checked = 0
t0 = time.time()
name_cache = {}

for code in all_codes:
    checked += 1
    bars = bars_by_code.get(code, [])
    if len(bars) < 42:
        continue

    # 确保数据到最新日期
    if bars[-1]["trade_date"] != latest:
        continue

    try:
        signals = generate_signals(bars)
    except Exception:
        continue

    for s in signals:
        if s["date"] == latest and s["action"] == "buy":
            # 评分
            closes = [b["close"] for b in bars]
            volumes = [b.get("volume") or 0 for b in bars]
            idx = len(closes) - 1
            close = closes[idx]
            ma20_arr = sma(closes, 20)
            vol_ma5 = sma(volumes, 5)
            vol_ma20_arr = sma(volumes, 20)
            vr = vol_ma5[idx] / vol_ma20_arr[idx] if vol_ma20_arr[idx] > 0 else 0
            chg_5d = (close - closes[idx-5]) / closes[idx-5] * 100 if idx >= 5 else 0
            chg_10d = (close - closes[idx-10]) / closes[idx-10] * 100 if idx >= 10 else 0
            chg_20d = (close - closes[idx-20]) / closes[idx-20] * 100 if idx >= 20 else 0
            dyn = _compute_price_volume_dynamics(closes, volumes, idx)
            dist_ma20 = (close - ma20_arr[idx]) / ma20_arr[idx]

            score_trend = min(30, max(0, chg_5d * 2 + (chg_20d > 0) * 10))
            score_vol = min(25, max(0, (vr - 1.3) / 2.7 * 25))
            score_coord = min(25, max(0, (dyn["up_vol_ratio"] - 1.1) / 1.4 * 25))
            score_pos = 20
            if dist_ma20 < 0.01: score_pos -= 5
            elif dist_ma20 > 0.10: score_pos -= 10
            if dyn["consecutive_up"] < 2: score_pos -= 5
            total_score = score_trend + score_vol + score_coord + score_pos

            # 名称
            if code not in name_cache:
                info = sess.get(StockInfo, code)
                name_cache[code] = info.name if info else code

            buy_signals.append({
                "code": code,
                "name": name_cache[code],
                "close": round(close, 2),
                "chg_5d": round(chg_5d, 1),
                "chg_10d": round(chg_10d, 1),
                "chg_20d": round(chg_20d, 1),
                "vol_ratio": round(vr, 1),
                "up_vol_ratio": round(dyn["up_vol_ratio"], 1),
                "consecutive_up": dyn["consecutive_up"],
                "dist_ma20": round(dist_ma20 * 100, 1),
                "score": round(total_score, 1),
                "reason": (
                    f"趋势跟随(5日{chg_5d:.1f}% 20日{chg_20d:.1f}% | "
                    f"量比{vr:.1f}x 涨跌量比{dyn['up_vol_ratio']:.1f}x | "
                    f"连涨{dyn['consecutive_up']}天)"
                ),
            })

    if checked % 500 == 0:
        elapsed = time.time() - t0
        print(f"  已扫 {checked}/{total} ({checked/total*100:.0f}%)  |  已找到 {len(buy_signals)} 只  |  {elapsed:.0f}s")

elapsed = time.time() - t0
print(f"\n扫描完成: {checked} 只  |  {elapsed:.0f}s")
print(f"趋势跟随 {latest} 买入信号: {len(buy_signals)} 只")

# 按评分排序
buy_signals.sort(key=lambda s: s["score"], reverse=True)

# 展示前10
print(f"\n{'─'*60}")
print(f"  排名  |  代码      |  名称    |  评分  |  5日涨  |  量比  |  涨跌量比")
print(f"{'─'*60}")
for i, s in enumerate(buy_signals[:20]):
    print(f"  {i+1:>3}   | {s['code']:<10} | {s['name']:<6} | {s['score']:>5.0f} | {s['chg_5d']:>+6.1f}% | {s['vol_ratio']:.1f}x | {s['up_vol_ratio']:.2f}")

# 写入 trade_history.json
history_file = ROOT_DIR / "data" / "trade_history.json"
with open(history_file, "r", encoding="utf-8") as f:
    history = json.load(f)

for h in history:
    if h["date"] == latest:
        h["buy_signals"] = buy_signals
        break
else:
    history.append({
        "date": latest, "cash": 400000, "holding_value": 0,
        "total_value": 400000, "sells": [], "keeps": [],
        "buy_signals": buy_signals, "position_count": 0,
    })

with open(history_file, "w", encoding="utf-8") as f:
    json.dump(history, f, ensure_ascii=False, indent=2)

print(f"\n已写入 trade_history.json ({len(buy_signals)} 条买入信号)")
sess.close()
