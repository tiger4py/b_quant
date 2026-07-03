"""用 Alpha042 量价背离指标分析 国瓷材料 300285"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

code = "sz.300285"

# 取最近 200 天数据
bars = sess.query(StockDaily).filter(
    StockDaily.code == code
).order_by(StockDaily.trade_date.asc()).all()

bars = [{"trade_date": b.trade_date, "open": float(b.open), "high": float(b.high),
         "low": float(b.low), "close": float(b.close),
         "volume": int(b.volume or 0), "amount": float(b.amount or 0)}
        for b in bars]

sess.close()

# 计算 alpha042 指标（直接用策略代码）
closes = [b["close"] for b in bars]
highs = [b["high"] for b in bars]
volumes = [b["volume"] for b in bars]
amounts = [b["amount"] for b in bars]
dates = [b["trade_date"] for b in bars]
n = len(bars)

# 波动率
daily_vol = [0.0]
for i in range(1, n):
    chg = abs(closes[i] / closes[i-1] - 1) if closes[i-1] else 0
    daily_vol.append(chg)

# SMA
def sma(values, period):
    result = [None] * len(values)
    s = 0
    for i in range(len(values)):
        if values[i] is None: continue
        s += values[i]
        if i >= period:
            s -= values[i - period]
        if i + 1 >= period:
            result[i] = s / period
    return result

vol_short = sma(daily_vol, 10)
vol_long = sma(daily_vol, 60)

vol_amp = [None] * n
for i in range(n):
    if vol_short[i] and vol_long[i] and vol_long[i] > 0.0001:
        vol_amp[i] = vol_short[i] / vol_long[i]

# corr(high, volume, 10)
def rolling_corr(x, y, window):
    result = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < window: continue
        xw = x[i+1-window:i+1]
        yw = y[i+1-window:i+1]
        valid = [(xv, yv) for xv, yv in zip(xw, yw) if xv is not None and yv is not None]
        nn = len(valid)
        if nn < 3: continue
        sx = sum(v[0] for v in valid); sy = sum(v[1] for v in valid)
        sxy = sum(v[0]*v[1] for v in valid)
        sx2 = sum(v[0]**2 for v in valid); sy2 = sum(v[1]**2 for v in valid)
        num = nn*sxy - sx*sy
        inner = (nn*sx2 - sx**2) * (nn*sy2 - sy**2)
        result[i] = num / (inner**0.5) if inner > 0 else 0.0
    return result

corr = rolling_corr(highs, volumes, 10)

# 20日高点
def rolling_max(vals, w):
    result = [None]*len(vals)
    for i in range(len(vals)):
        if i+1<w: continue
        result[i] = max(vals[i+1-w:i+1])
    return result

high_20 = rolling_max(highs, 20)

# 5日涨跌
chg_5d = [None]*n
for i in range(n):
    if i>=5 and closes[i-5]>0:
        chg_5d[i] = (closes[i]-closes[i-5])/closes[i-5]

# ======== 输出分析 ========
print("=" * 70)
print("国瓷材料 (300285) — Alpha042 量价背离分析")
print("=" * 70)

# 最近 20 天详细
print(f"\n{'日期':12s} {'收盘':>8s} {'涨幅':>8s} {'corr(10)':>9s} {'波放':>6s} {'距20高':>8s} {'5日%':>8s} {'量(万手)':>10s} {'信号'}")
print("-" * 95)

signals = []
for i in range(max(0, n-20), n):
    c = closes[i]
    corr_val = corr[i]
    va = vol_amp[i]
    h20 = high_20[i]
    chg5 = chg_5d[i]
    vol_wan = volumes[i]/10000
    amt = amounts[i]/1e8

    chg_str = ""
    if i > 0 and closes[i-1] > 0:
        chg_str = f"{(c/closes[i-1]-1)*100:+.2f}%"

    corr_str = f"{corr_val:.3f}" if corr_val is not None else "-"
    va_str = f"{va:.1f}x" if va is not None else "-"
    h20_str = f"{(c/h20-1)*100:+.1f}%" if h20 is not None else "-"
    chg5_str = f"{chg5*100:+.1f}%" if chg5 is not None else "-"

    # 判断 alpha042 信号
    sig = ""
    corr_sell = corr_val is not None and corr_val > 0.50
    corr_buy = corr_val is not None and corr_val < -0.25
    vol_ok = va is not None and 1.2 <= va <= 5.0
    near_high = h20 is not None and c >= h20 * 0.9
    not_falling = chg5 is not None and chg5 > -0.05

    if corr_sell:
        sig = "[卖出!!]量价同步"
    elif corr_buy and vol_ok and near_high and not_falling:
        sig = "[买入信号]"
    elif corr_buy:
        sig = "[缩量背离]"
    elif corr_val is not None and corr_val > 0.3:
        sig = "[量价趋同!]"

    print(f"{dates[i]} {c:>8.2f} {chg_str:>8s} {corr_str:>9s} {va_str:>6s} {h20_str:>8s} {chg5_str:>8s} {vol_wan:>10.0f} {sig}")

    if sig:
        signals.append((dates[i], sig, corr_val, c))

# 总结
print()
print("=" * 70)
print("判断")
print("=" * 70)

# 找最近的 corr 值
latest_corr = None
for i in range(n-1, -1, -1):
    if corr[i] is not None:
        latest_corr = corr[i]
        break

# 统计最近 5 天的 corr
recent_corrs = []
for i in range(max(0, n-5), n):
    if corr[i] is not None:
        recent_corrs.append(corr[i])

print(f"\n关键指标:")
print(f"  最新 corr(high, vol, 10) = {latest_corr:.3f}")
print(f"  最近5天 corr 均值 = {sum(recent_corrs)/len(recent_corrs):.3f}" if recent_corrs else "")
print(f"  Alpha042 卖出阈值: corr > 0.50")

if latest_corr is not None:
    if latest_corr > 0.50:
        print(f"\n  [卖出] corr={latest_corr:.3f} > 0.50 -> 量价同步，散户涌入，应该卖出！")
    elif latest_corr > 0.30:
        print(f"\n  [警告] corr={latest_corr:.3f}，逼近 0.50 卖出线，量价关系恶化中")
    elif latest_corr < -0.25:
        print(f"\n  [持有] corr={latest_corr:.3f} < -0.25 -> 缩量上涨，筹码锁定，健康")
    else:
        print(f"\n  [中性] corr={latest_corr:.3f}，中性区间")

# 价格 vs MA20
ma20 = sum(closes[-20:])/20 if len(closes)>=20 else 0
print(f"\n价格位置:")
print(f"  当前价: {closes[-1]:.2f}")
print(f"  MA20: {ma20:.2f}")
print(f"  距MA20: {(closes[-1]/ma20-1)*100:+.1f}%")
print(f"  20日最高: {high_20[-1]:.2f}" if high_20[-1] else "")

# 波动率
if vol_amp[-1]:
    print(f"\n波动率放大器:")
    print(f"  vol_10d/vol_60d = {vol_amp[-1]:.2f}x")
    if vol_amp[-1] > 5:
        print(f"  [警告] 波动率过高({vol_amp[-1]:.1f}x > 5x)，异常波动")
    elif vol_amp[-1] > 2:
        print(f"  [警告] 高波动({vol_amp[-1]:.1f}x)，筹码松动")
    elif vol_amp[-1] >= 1.2:
        print(f"  [正常] 波动适中({vol_amp[-1]:.1f}x)，信号可靠")
    else:
        print(f"  低波动，信号可信度下降")
