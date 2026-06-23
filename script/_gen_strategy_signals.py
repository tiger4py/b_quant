#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用趋势跟随策略跑出6/22持仓，写入 trade_history.json"""
import sys, json
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo
from backtest.strategy.strategy_trend_following import generate_signals

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

latest = sess.query(func.max(StockDaily.trade_date)).scalar()
print(f"最新数据日期: {latest}")

# 成交额前300活跃股
top_codes = [
    r[0] for r in sess.query(StockDaily.code)
    .join(StockInfo, StockDaily.code == StockInfo.code)
    .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date == latest)
    .order_by(StockDaily.amount.desc()).limit(300).all()
]
print(f"扫描活跃股: {len(top_codes)} 只")

# 加载K线
bars_by_code = {}
for code in top_codes:
    rows = (
        sess.query(StockDaily)
        .filter(StockDaily.code == code)
        .order_by(StockDaily.trade_date.asc())
        .all()
    )
    if len(rows) >= 45:
        bars_by_code[code] = [
            {
                "trade_date": r.trade_date, "open": r.open, "high": r.high,
                "low": r.low, "close": r.close, "volume": r.volume, "amount": r.amount,
            }
            for r in rows
        ]

# 跑 generate_signals，收集6/22的买入信号
buy_signals = []
for code, bars in bars_by_code.items():
    try:
        signals = generate_signals(bars)
    except Exception:
        continue
    for s in signals:
        if s["date"] == latest and s["action"] == "buy":
            name_row = sess.get(StockInfo, code)
            name = name_row.name if name_row else code
            last_bar = bars[-1]

            # 计算评分（复用 _check_buy_today 的逻辑）
            from backtest.indicators import sma
            closes = [b["close"] for b in bars]
            volumes = [b.get("volume") or 0 for b in bars]
            idx = len(closes) - 1
            close = closes[idx]
            ma20_arr = sma(closes, 20)
            vol_ma5 = sma(volumes, 5)
            vol_ma20_arr = sma(volumes, 20)
            vol_ratio = vol_ma5[idx] / vol_ma20_arr[idx] if vol_ma20_arr[idx] > 0 else 0
            chg_5d = (close - closes[idx - 5]) / closes[idx - 5] * 100 if idx >= 5 else 0
            chg_20d = (close - closes[idx - 20]) / closes[idx - 20] * 100 if idx >= 20 else 0

            from backtest.strategy.strategy_trend_following import _compute_price_volume_dynamics
            dyn = _compute_price_volume_dynamics(closes, volumes, idx)

            buy_signals.append({
                "code": code,
                "name": name,
                "close": round(close, 2),
                "chg_5d": round(chg_5d, 1),
                "chg_20d": round(chg_20d, 1),
                "vol_ratio": round(vol_ratio, 1),
                "up_vol_ratio": round(dyn["up_vol_ratio"], 1),
                "consecutive_up": dyn["consecutive_up"],
                "score": 0,  # 暂不评分
                "reason": s["reason"],
            })

print(f"\n趋势跟随 6/22 买入信号: {len(buy_signals)} 只")
for i, s in enumerate(buy_signals):
    print(f"  {i+1}. {s['code']} {s['name']}  @{s['close']}  5日{s['chg_5d']:+.1f}%  量比{s['vol_ratio']}x  {s['reason']}")

# 写入 trade_history.json
history_file = ROOT_DIR / "data" / "trade_history.json"
with open(history_file, "r", encoding="utf-8") as f:
    history = json.load(f)

# 更新最新一条
for h in history:
    if h["date"] == latest:
        h["buy_signals"] = buy_signals
        break
else:
    history.append({
        "date": latest,
        "cash": 400000,
        "holding_value": 0,
        "total_value": 400000,
        "sells": [],
        "keeps": [],
        "buy_signals": buy_signals,
        "position_count": 0,
    })

with open(history_file, "w", encoding="utf-8") as f:
    json.dump(history, f, ensure_ascii=False, indent=2)

print(f"\n已写入 trade_history.json ({len(buy_signals)} 条买入信号)")
sess.close()
