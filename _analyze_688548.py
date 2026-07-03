"""Alpha042 分析 688548"""
import sys, os, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

code = "sh.688548"

# 基本信息
stock = sess.query(StockInfo).filter(StockInfo.code == code).first()
name = stock.name if stock else "?"

# 取最近 200 天
bars = sess.query(StockDaily).filter(
    StockDaily.code == code
).order_by(StockDaily.trade_date.asc()).all()

# 转成 (date, close, high, low, open, volume)
data = [(b.trade_date, float(b.close), float(b.high), float(b.low),
         float(b.open), int(b.volume or 0)) for b in bars]
sess.close()

# 尝试获取今天实时数据
today_data = None
try:
    url = "https://hq.sinajs.cn/list=sh688548"
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read().decode("gbk")
    parts = raw.split('"')[1].split(",")
    if len(parts) >= 9:
        today_data = ("2026-07-03", float(parts[3]), float(parts[4]),
                      float(parts[5]), float(parts[1]), int(parts[8]))
except:
    pass

if today_data:
    data.append(today_data)
    data.sort(key=lambda x: x[0])

# 取最近 100 天做完整计算
recent_full = data[-100:]
n = len(recent_full)
closes_full = [d[1] for d in recent_full]
highs_full = [d[2] for d in recent_full]
volumes_full = [d[5] for d in recent_full]
dates_full = [d[0] for d in recent_full]

# corr(high, vol, 10)
def rolling_corr(x, y, w):
    result = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < w: continue
        xw = x[i+1-w:i+1]; yw = y[i+1-w:i+1]
        valid = [(xv, yv) for xv, yv in zip(xw, yw)]
        nn = len(valid)
        if nn < 3: continue
        sx = sum(v[0] for v in valid); sy = sum(v[1] for v in valid)
        sxy = sum(v[0]*v[1] for v in valid)
        sx2 = sum(v[0]**2 for v in valid); sy2 = sum(v[1]**2 for v in valid)
        num = nn*sxy - sx*sy
        inner = (nn*sx2 - sx**2) * (nn*sy2 - sy**2)
        result[i] = num / (inner**0.5) if inner > 0 else 0.0
    return result

corr_full = rolling_corr(highs_full, volumes_full, 10)

# 波动率
daily_vol = [0.0]
for i in range(1, n):
    chg = abs(closes_full[i]/closes_full[i-1]-1) if closes_full[i-1] else 0
    daily_vol.append(chg)

def sma(vals, period):
    result = [None]*len(vals)
    s=0
    for i in range(len(vals)):
        if vals[i] is None: continue
        s+=vals[i]
        if i>=period: s-=vals[i-period]
        if i+1>=period: result[i]=s/period
    return result

vs = sma(daily_vol, 10)
vl = sma(daily_vol, 60)
vol_amp = [None]*n
for i in range(n):
    if vs[i] and vl[i] and vl[i]>0.0001:
        vol_amp[i] = vs[i]/vl[i]

# 20日高点
def rolling_max(vals, w):
    result=[None]*len(vals)
    for i in range(len(vals)):
        if i+1<w: continue
        result[i] = max(vals[i+1-w:i+1])
    return result
h20 = rolling_max(highs_full, 20)

# 5日涨跌
chg5 = [None]*n
for i in range(n):
    if i>=5 and closes_full[i-5]>0:
        chg5[i] = (closes_full[i]-closes_full[i-5])/closes_full[i-5]

# ======== 输出 ========
print(f"股票: {code} {name}")
print(f"数据范围: {dates_full[0]} ~ {dates_full[-1]} ({n}条)")
if today_data:
    print(f"今日实时: 已获取")
else:
    print(f"今日实时: 未获取(可能未收盘)")

# 最近 15 天详细
print(f"\n{'日期':12s} {'收盘':>8s} {'涨幅':>8s} {'corr':>8s} {'波放':>6s} {'距20高':>8s} {'5日%':>8s} {'量(万手)':>10s} {'信号'}")
print("-" * 85)

for i in range(max(0, n-15), n):
    c = closes_full[i]; h = highs_full[i]; v = volumes_full[i]
    cv = corr_full[i]; va = vol_amp[i]; hh = h20[i]; c5 = chg5[i]
    chg_str = f"{(c/closes_full[i-1]-1)*100:+.2f}%" if i>0 and closes_full[i-1]>0 else ""
    cv_str = f"{cv:.3f}" if cv is not None else "-"
    va_str = f"{va:.1f}x" if va is not None else "-"
    hh_str = f"{(c/hh-1)*100:+.1f}%" if hh is not None else "-"
    c5_str = f"{c5*100:+.1f}%" if c5 is not None else "-"
    v_str = f"{v/10000:.0f}"

    sig = ""
    if cv is not None:
        if cv > 0.50: sig = ">> [卖出!!]"
        elif cv > 0.30: sig = "> [趋同]"
        elif cv < -0.25: sig = "<< [背离]"
        else: sig = "-"

    marker = " <-- 今天" if dates_full[i] == "2026-07-03" else ""
    print(f"{dates_full[i]} {c:>8.2f} {chg_str:>8s} {cv_str:>8s} {va_str:>6s} {hh_str:>8s} {c5_str:>8s} {v_str:>10s} {sig}{marker}")

# 综合判断
print()
print("=" * 60)
print("综合判断")
print("=" * 60)

latest_corr = None
for i in range(n-1, -1, -1):
    if corr_full[i] is not None:
        latest_corr = corr_full[i]
        break

ma5 = sum(closes_full[-5:])/5 if n>=5 else 0
ma10 = sum(closes_full[-10:])/10 if n>=10 else 0
ma20 = sum(closes_full[-20:])/20 if n>=20 else 0

print(f"  最新价: {closes_full[-1]:.2f}")
print(f"  MA5: {ma5:.2f} | MA10: {ma10:.2f} | MA20: {ma20:.2f}")
print(f"  最新 corr: {latest_corr:.3f}" if latest_corr else "")
print(f"  波动放大: {vol_amp[-1]:.1f}x" if vol_amp[-1] else "")
print(f"  距20日高: {(closes_full[-1]/h20[-1]-1)*100:+.1f}%" if h20[-1] else "")
print(f"  5日涨跌: {chg5[-1]*100:+.1f}%" if chg5[-1] else "")

# corr 趋势
print(f"\n  corr 变化:")
recent_corr = [(dates_full[i], corr_full[i]) for i in range(n) if corr_full[i] is not None]
for i in range(max(0, len(recent_corr)-7), len(recent_corr)):
    print(f"    {recent_corr[i][0]}: corr={recent_corr[i][1]:.3f}")

# 结论
print()
if latest_corr is not None:
    if latest_corr > 0.50:
        print("  [卖出] corr > 0.50，量价同步 -> 跑！")
    elif latest_corr > 0.30:
        print("  [警告] corr 高位，量价趋同 -> 减仓观察")
    elif latest_corr < -0.25:
        price_above_ma5 = closes_full[-1] > ma5
        near_20high = h20[-1] and (closes_full[-1]/h20[-1]) > 0.9
        if price_above_ma5 and near_20high:
            print("  [持有] 缩量背离 + 强势位置 -> 安心持有")
        else:
            print("  [持有偏弱] 缩量背离但位置走弱 -> 持有但设止损")
    else:
        if closes_full[-1] < ma5:
            print("  [观望] corr中性 + 跌破MA5 -> 不建议加仓")
        else:
            print("  [中性] corr中性，方向不明 -> 观望")
