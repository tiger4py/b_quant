#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最简化脚本：下载60天K线 → 价增量增扫描 → QQ推送前3只"""
import sys, os, time, json
from datetime import datetime, timedelta
from collections import defaultdict

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 设置路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ========== 1. 下载 K 线数据 ==========
print("=" * 50)
print("Phase 1: 下载60天K线数据")
print("=" * 50)

import baostock as bs
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockInfo, StockDaily

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

# 获取股票列表
codes = [r[0] for r in sess.query(StockInfo.code)
         .filter(StockInfo.type == "1", StockInfo.status == 1)
         .order_by(StockInfo.code).all()]
print(f"股票总数: {len(codes)}")

end_date = datetime.now().strftime("%Y-%m-%d")
start_date = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
print(f"日期范围: {start_date} ~ {end_date}")

bs.login()
print("BaoStock 登录成功")

count = 0
t_start = time.time()

for idx, code in enumerate(codes, 1):
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume,amount,turn,peTTM",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="3"
        )
        if rs.error_code != "0":
            if idx <= 3:
                print(f"  [{idx}] {code}: error={rs.error_msg}")
            continue

        data = rs.get_data()
        if data.empty:
            continue

        for row in data.values.tolist():
            td = str(row[0])[:10]
            existing = sess.query(StockDaily).filter_by(code=code, trade_date=td).first()

            # 安全转换
            def sf(v):
                try:
                    if v is None or (isinstance(v, float) and v != v):
                        return None
                    return float(v)
                except (ValueError, TypeError):
                    return None

            def si(v):
                try:
                    if v is None or (isinstance(v, float) and v != v):
                        return None
                    return int(float(v))
                except (ValueError, TypeError):
                    return None

            vals = {
                "open": sf(row[1]), "high": sf(row[2]), "low": sf(row[3]),
                "close": sf(row[4]), "volume": si(row[5]), "amount": sf(row[6]),
                "turn": sf(row[7]), "pe_ttm": sf(row[8]),
            }

            if existing:
                for k, v in vals.items():
                    setattr(existing, k, v)
            else:
                sess.add(StockDaily(code=code, trade_date=td, **vals))
            count += 1

        if idx % 100 == 0:
            sess.commit()
            elapsed = time.time() - t_start
            rate = idx / elapsed
            eta_min = (len(codes) - idx) / rate / 60
            print(f"  进度: {idx}/{len(codes)} ({100*idx/len(codes):.0f}%), "
                  f"速度 {rate:.1f}只/秒, 剩余 {eta_min:.0f}分钟, 写入 {count} 行")
            sys.stdout.flush()

    except Exception as e:
        if idx <= 3:
            print(f"  [{idx}] {code}: 异常 - {e}")

sess.commit()
bs.logout()
elapsed = time.time() - t_start
print(f"下载完成: {count} 行, {len(codes)} 只股票, 耗时 {elapsed/60:.1f}分钟")
sys.stdout.flush()

# ========== 2. 价增量增扫描 ==========
print("")
print("=" * 50)
print("Phase 2: 价增量增策略扫描")
print("=" * 50)

from backtest.indicators import sma

# 加载最新数据
latest_date = sess.query(StockDaily.trade_date).order_by(StockDaily.trade_date.desc()).first()[0]
print(f"数据库最新日期: {latest_date}")

# 活跃股票
active = (
    sess.query(StockInfo, StockDaily)
    .join(StockDaily, StockInfo.code == StockDaily.code)
    .filter(StockInfo.type == "1", StockInfo.status == 1,
            StockDaily.trade_date == latest_date)
    .all()
)
stock_map = {s.code: {"code": s.code, "name": s.name, "market": s.market} for s, _ in active}
print(f"活跃股票: {len(stock_map)} 只")

# 加载K线
dates = (
    sess.query(StockDaily.trade_date).distinct()
    .order_by(StockDaily.trade_date.desc()).limit(200).all()
)
cutoff = min(d[0] for d in dates)

rows = (
    sess.query(StockDaily)
    .join(StockInfo, StockDaily.code == StockInfo.code)
    .filter(StockInfo.type == "1", StockInfo.status == 1,
            StockDaily.trade_date >= cutoff)
    .order_by(StockDaily.code, StockDaily.trade_date)
    .all()
)

bars_by_code = defaultdict(list)
for r in rows:
    bars_by_code[r.code].append({
        "trade_date": r.trade_date, "open": r.open, "high": r.high,
        "low": r.low, "close": r.close, "volume": r.volume, "amount": r.amount,
    })
print(f"加载 {len(bars_by_code)} 只股票K线")

# ========== 策略参数 ==========
PRICE_UP_5D_MIN = 0.02       # 5日涨幅至少2%（趋势延续，不是超跌反弹）
PRICE_UP_20D_MIN = 0.00      # 20日涨幅至少0%（中长期不跌）
PRICE_DOWN_40D_MAX = -0.15   # 40日最大跌幅不超过15%（排除深跌后反弹）
VOL_RATIO_BUY = 1.5
VOL_RATIO_MAX = 4.0
VOL_TREND_ACCEL = 1.1
UP_VOL_RATIO = 1.2
PRICE_ABOVE_MA20_MAX = 1.08  # 收盘不超MA20的8%（不追高）
PRICE_NEAR_20D_HIGH = 0.06   # 距20日高点不超过6%（强势股特征）
MIN_CONSEC_UP = 2
LOOKBACK_DAYS = 10

def check_buy(closes, volumes, highs, i, ma10, ma20, vol_ma5, vol_ma20):
    if ma10[i] is None or ma20[i] is None or vol_ma5[i] is None or vol_ma20[i] is None:
        return None
    if vol_ma20[i] == 0:
        return None

    close = closes[i]
    vol_ratio = vol_ma5[i] / vol_ma20[i]

    # ---- 趋势过滤（核心：排除触底反弹）----
    # 5日涨幅
    if i < 5 or closes[i - 5] <= 0:
        return None
    chg_5d = (close - closes[i - 5]) / closes[i - 5]
    if chg_5d < PRICE_UP_5D_MIN:
        return None

    # 20日涨幅：中长期必须不跌（排除健民集团这种跌30%反弹的）
    if i >= 20 and closes[i - 20] > 0:
        chg_20d = (close - closes[i - 20]) / closes[i - 20]
        if chg_20d < PRICE_UP_20D_MIN:
            return None

    # 40日最大跌幅：如果40日内有过深跌，说明是底部反弹，不参与
    if i >= 40:
        lookback_40 = closes[i - 39:i + 1]
        peak_40 = max(lookback_40)
        trough_40 = min(lookback_40)
        max_drawdown_40 = (trough_40 - peak_40) / peak_40
        if max_drawdown_40 < PRICE_DOWN_40D_MAX:
            return None

    # ---- 趋势位置：必须处于强势区域 ----
    # 站上MA10 且 站上MA20
    if close <= ma10[i] or close <= ma20[i]:
        return None

    # 距20日高点不超过6%（强势股接近新高，不是底部趴着的）
    if i >= 20:
        high_20 = max(highs[i - 19:i + 1])
        if (high_20 - close) / high_20 > PRICE_NEAR_20D_HIGH:
            return None

    # 不追高：收盘不超过MA20的8%
    if close > ma20[i] * PRICE_ABOVE_MA20_MAX:
        return None

    # ---- 量能过滤 ----
    # 量能放大
    if vol_ratio < VOL_RATIO_BUY or vol_ratio > VOL_RATIO_MAX:
        return None

    # 量能加速
    if i >= 6:
        recent_3 = sum(volumes[i - 2:i + 1]) / 3
        prior_3 = sum(volumes[i - 5:i - 2]) / 3
        if prior_3 > 0 and recent_3 / prior_3 < VOL_TREND_ACCEL:
            return None

    # 量价配合
    start = max(1, i - LOOKBACK_DAYS + 1)
    up_vols, down_vols = [], []
    up_days = down_days = 0
    for j in range(start, i + 1):
        chg = closes[j] - closes[j - 1]
        vol = volumes[j] if volumes[j] else 0
        if chg > 0:
            up_vols.append(vol); up_days += 1
        elif chg < 0:
            down_vols.append(vol); down_days += 1

    up_avg = sum(up_vols) / len(up_vols) if up_vols else 0
    down_avg = sum(down_vols) / len(down_vols) if down_vols else 1
    up_vol_ratio = up_avg / down_avg if down_avg > 0 else 2.0
    if up_vol_ratio < UP_VOL_RATIO:
        return None

    # 连续上涨
    consecutive_up = 0
    for j in range(i, 0, -1):
        if closes[j] > closes[j - 1]:
            consecutive_up += 1
        else:
            break
    if consecutive_up < MIN_CONSEC_UP:
        return None

    dist_ma20 = (close - ma20[i]) / ma20[i]
    chg_10d = (close - closes[i - 10]) / closes[i - 10] if i >= 10 and closes[i - 10] > 0 else 0
    chg_20d_val = (close - closes[i - 20]) / closes[i - 20] if i >= 20 and closes[i - 20] > 0 else 0

    return {
        "code": "", "name": "", "market": "",
        "chg_5d": chg_5d, "chg_10d": chg_10d, "chg_20d": chg_20d_val,
        "close": close, "ma20": round(ma20[i], 2), "dist_ma20": dist_ma20,
        "vol_ratio": vol_ratio, "up_vol_ratio": up_vol_ratio,
        "up_days": up_days, "down_days": down_days, "consecutive_up": consecutive_up,
        "daily_chg": (closes[i] - closes[i - 1]) / closes[i - 1] if i > 0 else 0,
    }

# 扫描
candidates = []
scanned = skipped = 0
for code, bars in bars_by_code.items():
    if len(bars) < 25:
        skipped += 1
        continue
    scanned += 1

    if bars[-1]["trade_date"] != latest_date:
        continue

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)
    idx = len(closes) - 1

    result = check_buy(closes, volumes, highs, idx, ma10, ma20, vol_ma5, vol_ma20)
    if result is None:
        continue

    stock = stock_map.get(code, {"code": code, "name": code, "market": ""})
    result["code"] = code
    result["name"] = stock.get("name", code)
    result["market"] = stock.get("market", "")
    candidates.append(result)

print(f"扫描: {scanned} 只, 跳过(<25天): {skipped} 只, 候选: {len(candidates)} 只")

# 评分排序
def clamp(v, lo, hi):
    return max(lo, min(hi, v))

for c in candidates:
    # 价格趋势强度 (30%)
    chg_pct = c["chg_5d"] * 100
    sp = clamp((chg_pct - 1) / 9 * 80, 0, 80)
    if c["chg_10d"] > 0.02: sp += 10
    if c["chg_20d"] > 0: sp += 10
    c["score_price"] = clamp(sp, 0, 100)

    # 量能强度 (30%)
    vr = c["vol_ratio"]
    sv = (vr - 1.5) / 2.0 * 100 if vr <= 3.5 else 100 - (vr - 3.5) / 0.5 * 40
    c["score_volume"] = clamp(sv, 0, 100)

    # 量价配合 (25%)
    sc = (c["up_vol_ratio"] - 1.2) / 1.3 * 100
    if c["up_days"] >= 7: sc += 10
    c["score_coordination"] = clamp(sc, 0, 100)

    # 趋势质量 (15%)
    cu = c["consecutive_up"]
    sq = clamp((cu - 2) / 3 * 60, 0, 60)
    dist = c["dist_ma20"] * 100
    if 1.0 <= dist <= 3.0: sq += 40
    elif 0 < dist < 1.0: sq += dist / 1.0 * 30
    elif 3.0 < dist <= 8.0: sq += (8.0 - dist) / 5.0 * 30
    c["score_quality"] = clamp(sq, 0, 100)

    c["score"] = c["score_price"] * 0.30 + c["score_volume"] * 0.30 \
                 + c["score_coordination"] * 0.25 + c["score_quality"] * 0.15

candidates.sort(key=lambda x: x["score"], reverse=True)
for i, c in enumerate(candidates, 1):
    c["rank"] = i

if candidates:
    print(f"最高评分: {candidates[0]['score']:.0f} (共 {len(candidates)} 只)")
else:
    print("无符合条件的候选股")
    sess.close()
    sys.exit(0)

# 展示前3
print("")
print("=" * 50)
print("前3只候选股:")
print("=" * 50)
for c in candidates[:3]:
    print(f"[{c['rank']}] {c['code']} {c['name']} — 评分 {c['score']:.0f}")
    print(f"  5日涨 {c['chg_5d']*100:+.1f}% | 10日涨 {c['chg_10d']*100:+.1f}% | 20日涨 {c['chg_20d']*100:+.1f}%")
    print(f"  量比 {c['vol_ratio']:.1f}x | 涨跌量比 {c['up_vol_ratio']:.1f}x | 连涨 {c['consecutive_up']}天")
    print(f"  距MA20 {c['dist_ma20']*100:+.1f}% | 今日 {c['daily_chg']*100:+.1f}%")
    print(f"  分项: 价格{c['score_price']:.0f} 量能{c['score_volume']:.0f} 配合{c['score_coordination']:.0f} 质量{c['score_quality']:.0f}")
    sys.stdout.flush()

# ========== 3. QQ推送 ==========
print("")
print("=" * 50)
print("Phase 3: QQ推送")
print("=" * 50)

def weekday(d):
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{d} ({['周一','周二','周三','周四','周五','周六','周日'][dt.weekday()]})"
    except:
        return d

TOP_N = 3
lines = [
    f"[*] 价增量增策略 — {weekday(latest_date)}",
    f"候选: {len(candidates)}只 | 展示前{TOP_N}只",
    "",
]

for c in candidates[:TOP_N]:
    lines.append(
        f"{c['rank']}. {c['name']}({c['code']}) "
        f"评分{c['score']:.0f} | 5日涨{c['chg_5d']*100:+.1f}% "
        f"| 量比{c['vol_ratio']:.1f}x | 连涨{c['consecutive_up']}天 "
        f"| 涨跌量比{c['up_vol_ratio']:.1f}x"
    )

lines.append("")
lines.append("--- 价增量增策略 · 仅供参考 ---")
msg = "\n".join(lines)

print(msg)

# 推送QQ
try:
    from models.qq_webhook import QQPusher
    pusher = QQPusher()
    if pusher.enabled:
        result = pusher.push_long_text(msg)
        print(f"\nQQ推送: success={result['success']}, fail={result['fail']}")
    else:
        print("\nQQ推送未启用")
except Exception as e:
    print(f"\nQQ推送异常: {e}")

sess.close()
print("\n全部完成!")
