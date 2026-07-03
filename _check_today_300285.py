"""查询 300285 今天(2026-07-03)行情 + Alpha042 corr 判断"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

code = "sz.300285"
latest_date = sess.query(func.max(StockDaily.trade_date)).filter(
    StockDaily.code == code).scalar()
print(f"数据库最新日期: {latest_date}")

# 取最近 100 天
bars = sess.query(StockDaily).filter(
    StockDaily.code == code
).order_by(StockDaily.trade_date.asc()).all()

db_data = [(b.trade_date, float(b.close), float(b.high), float(b.low),
            float(b.open), int(b.volume or 0), float(b.amount or 0))
           for b in bars]
sess.close()

# 尝试拉取今天数据
today_data = None
today_str = "2026-07-03"

# 先看 DB 有没有
for d in db_data:
    if d[0] == today_str:
        today_data = d
        break

if not today_data:
    # 用 akshare 补救
    print("数据库无今天数据，从 AKShare 拉取...")
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol="300285", period="daily",
                                start_date="20260701", end_date="20260703", adjust="qfq")
        if len(df) > 0:
            row = df.iloc[-1]
            today_data = (str(row["日期"]), float(row["收盘"]), float(row["最高"]),
                         float(row["最低"]), float(row["开盘"]),
                         int(row["成交量"]), float(row["成交额"]))
            print(f"  拉到: {today_data[0]} close={today_data[1]} vol={today_data[5]}")
        else:
            print("  AKShare 今天还没数据（可能还没收盘或非交易日）")
    except Exception as e:
        print(f"  AKShare 拉取失败: {e}")

# 合并数据
all_data = list(db_data)
if today_data and today_data[0] not in {d[0] for d in all_data}:
    all_data.append(today_data)
    all_data.sort(key=lambda x: x[0])

closes = [d[1] for d in all_data]
highs = [d[2] for d in all_data]
volumes = [d[5] for d in all_data]
dates = [d[0] for d in all_data]

# ======== 计算 corr(high, vol, 10) ========
n = len(all_data)

def rolling_corr(x, y, w):
    result = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < w: continue
        xw = x[i+1-w:i+1]; yw = y[i+1-w:i+1]
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

# 波动率
daily_vol = [0.0]
for i in range(1, n):
    chg = abs(closes[i] / closes[i-1] - 1) if closes[i-1] else 0
    daily_vol.append(chg)

def sma(vals, period):
    result = [None] * len(vals)
    s = 0
    for i in range(len(vals)):
        if vals[i] is None: continue
        s += vals[i]
        if i >= period: s -= vals[i-period]
        if i + 1 >= period: result[i] = s / period
    return result

vs = sma(daily_vol, 10)
vl = sma(daily_vol, 60)
vol_amp = [None]*n
for i in range(n):
    if vs[i] and vl[i] and vl[i] > 0.0001:
        vol_amp[i] = vs[i] / vl[i]

# 20日高点
def rolling_max(vals, w):
    result = [None]*len(vals)
    for i in range(len(vals)):
        if i+1<w: continue
        result[i] = max(vals[i+1-w:i+1])
    return result
h20 = rolling_max(highs, 20)

# 5日涨跌
chg5 = [None]*n
for i in range(n):
    if i>=5 and closes[i-5]>0:
        chg5[i] = (closes[i]-closes[i-5])/closes[i-5]

# ======== 输出 ========
print()
print("=" * 65)
print(f"300285 国瓷材料 — Alpha042 实时诊断")
print("=" * 65)

# 最近 10 天
print(f"\n{'日期':12s} {'收盘':>8s} {'涨幅':>8s} {'corr':>8s} {'波放':>6s} {'距20高':>8s} {'5日%':>8s} {'信号'}")
print("-" * 75)

for i in range(max(0, n-10), n):
    c = closes[i]
    cv = corr[i]; va = vol_amp[i]; hh = h20[i]; c5 = chg5[i]
    chg_str = f"{(c/closes[i-1]-1)*100:+.2f}%" if i>0 and closes[i-1]>0 else ""
    cv_str = f"{cv:.3f}" if cv is not None else "-"
    va_str = f"{va:.1f}x" if va is not None else "-"
    hh_str = f"{(c/hh-1)*100:+.1f}%" if hh is not None else "-"
    c5_str = f"{c5*100:+.1f}%" if c5 is not None else "-"

    sig = ""
    if cv is not None:
        if cv > 0.50: sig = ">> 卖出!!(量价同步)"
        elif cv > 0.30: sig = "> 量价趋同"
        elif cv < -0.25: sig = "<< 缩量背离"
        else: sig = "-"
    print(f"{dates[i]} {c:>8.2f} {chg_str:>8s} {cv_str:>8s} {va_str:>6s} {hh_str:>8s} {c5_str:>8s} {sig}")

# 综合判断
print()
print("=" * 65)
print("综合判断")
print("=" * 65)

latest_corr = None
for i in range(n-1, -1, -1):
    if corr[i] is not None:
        latest_corr = corr[i]
        break

ma5 = sum(closes[-5:])/5 if n>=5 else 0
ma10 = sum(closes[-10:])/10 if n>=10 else 0

print(f"  最新价: {closes[-1]:.2f}")
print(f"  MA5: {ma5:.2f} | MA10: {ma10:.2f}")
print(f"  corr(high,vol,10): {latest_corr:.3f}")
print(f"  波动放大: {vol_amp[-1]:.1f}x" if vol_amp[-1] else "")
print(f"  距20日高: {(closes[-1]/h20[-1]-1)*100:+.1f}%" if h20[-1] else "")
print(f"  5日涨跌: {chg5[-1]*100:+.1f}%" if chg5[-1] else "")

print()
if latest_corr is not None:
    if latest_corr > 0.50:
        print("  [结论] corr > 0.50，量价同步 = 必须跑！")
    elif latest_corr > 0.30:
        print("  [结论] corr 在恶化，接近卖出阈值，建议减仓")
    elif latest_corr < -0.25:
        if closes[-1] < ma5:
            print("  [结论] corr OK但价格破MA5，缩量调整中，设止损继续持有")
        else:
            print("  [结论] 缩量背离 + 价格在MA5上方，继续持有")
    else:
        print("  [结论] corr 中性，信号不明确，观望")
