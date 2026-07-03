"""加入今天实时数据，重新算 corr"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

code = "sz.300285"
bars = sess.query(StockDaily).filter(
    StockDaily.code == code
).order_by(StockDaily.trade_date.asc()).all()

# 最近 15 天数据
data = [(b.trade_date, float(b.close), float(b.high), float(b.low),
         float(b.open), int(b.volume or 0)) for b in bars]
sess.close()

# 追加今天实时数据（来自新浪）
# 昨收 92.79, 今开 92.97, 最新 91.45, 最高 96.00, 最低 88.00, 量 88255390
today = ("2026-07-03", 91.45, 96.00, 88.00, 92.97, 88255390)
data.append(today)

# 取最近数据
recent = data[-15:]
closes = [d[1] for d in recent]
highs = [d[2] for d in recent]
volumes = [d[5] for d in recent]
dates = [d[0] for d in recent]

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

corr = rolling_corr(highs, volumes, 10)

# 5日涨跌
chg5 = [None]*len(recent)
for i in range(len(recent)):
    if i>=5 and closes[i-5]>0:
        chg5[i] = (closes[i]-closes[i-5])/closes[i-5]

# SMA for vol amp
daily_vol = [0.0]
for i in range(1, len(recent)):
    chg = abs(closes[i]/closes[i-1]-1) if closes[i-1] else 0
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
vl = sma(daily_vol, 60) if len(daily_vol) >= 60 else [None]*len(daily_vol)

# 输出最近几天
print("=" * 65)
print("300285 国瓷材料 — 含 07-03 实时 Alpha042 诊断")
print("=" * 65)
print(f"\n{'日期':12s} {'收盘':>8s} {'最高':>8s} {'涨跌':>8s} {'corr':>8s} {'量(万手)':>10s} {'信号'}")
print("-" * 70)

signals = []
for i in range(len(recent)):
    c = closes[i]; h = highs[i]; v = volumes[i]
    cv = corr[i]; c5 = chg5[i]
    chg_str = f"{(c/closes[i-1]-1)*100:+.2f}%" if i>0 and closes[i-1]>0 else ""
    cv_str = f"{cv:.3f}" if cv is not None else "-"
    v_str = f"{v/10000:.0f}"
    sig = ""
    if cv is not None:
        if cv > 0.50: sig = ">> [卖出!!]"
        elif cv > 0.30: sig = "> [趋同]"
        elif cv < -0.25: sig = "<< [背离]"
        else: sig = "-"
    print(f"{dates[i]} {c:>8.2f} {h:>8.2f} {chg_str:>8s} {cv_str:>8s} {v_str:>10s} {sig}")

# 最终判断
print()
print("=" * 65)
print("最终判断")
print("=" * 65)

latest_corr = corr[-1]
ma5 = sum(closes[-5:])/5
ma10 = sum(closes[-10:])/10

print(f"  今天: {dates[-1]}")
print(f"  最新价: {closes[-1]:.2f}  (昨收 92.79, 今跌 {(closes[-1]/92.79-1)*100:+.2f}%)")
print(f"  最高: {highs[-1]:.2f}  最低: {recent[-1][3]:.2f}")
print(f"  成交量: {volumes[-1]/10000:.0f}万手")
print(f"  MA5: {ma5:.2f} | MA10: {ma10:.2f}")
print(f"  corr(high,vol,10): {latest_corr:.3f}")
print(f"  5日涨跌: {chg5[-1]*100:+.1f}%" if chg5[-1] else "")

# 看 corr 趋势
recent_corrs = [(dates[i], corr[i]) for i in range(len(dates)) if corr[i] is not None]
print(f"\n  corr 变化趋势:")
for i in range(max(0, len(recent_corrs)-5), len(recent_corrs)):
    print(f"    {recent_corrs[i][0]}: corr={recent_corrs[i][1]:.3f}")

print()
if latest_corr > 0.50:
    print("  [结论] corr > 0.50，量价同步 -> 跑！")
elif latest_corr > 0.30:
    print("  [结论] corr 恶化中({:.3f})，逼近 0.50 卖出线 -> 减仓/跑".format(latest_corr))
elif latest_corr < -0.25:
    print(f"  [结论] corr={latest_corr:.3f} < -0.25，缩量背离 -> 继续持有观察")
else:
    print(f"  [结论] corr={latest_corr:.3f}，中性。但:")
    print(f"    - 盘中砸到 {recent[-1][3]:.2f} (跌 {recent[-1][3]/92.79*100-100:+.1f}%)")
    print(f"    - 连续3天跌破MA5")
    print(f"    - 如果明天再跌，corr大概率转正 -> 触发卖出")
    print(f"    -> 建议: 设止损 88元(今天最低)，破了就走")
