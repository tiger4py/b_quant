"""Alpha042 分析 301509 + 603127 — 含今日实时"""
import sys, os, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

TARGETS = [
    ("sz.301509", "301509", "金凯生科"),
    ("sh.603127", "603127", "昭衍新药"),
]

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

def sma(vals, period):
    result = [None]*len(vals)
    s=0
    for i in range(len(vals)):
        if vals[i] is None: continue
        s+=vals[i]
        if i>=period: s-=vals[i-period]
        if i+1>=period: result[i]=s/period
    return result

def rolling_max(vals, w):
    result=[None]*len(vals)
    for i in range(len(vals)):
        if i+1<w: continue
        result[i]=max(vals[i+1-w:i+1])
    return result

for db_code, sina_code, name in TARGETS:
    print()
    print("=" * 70)
    print(f"  {sina_code} {name} — Alpha042 量价诊断")
    print("=" * 70)

    # DB 历史数据
    bars = sess.query(StockDaily).filter(
        StockDaily.code == db_code
    ).order_by(StockDaily.trade_date.asc()).all()

    data = [(b.trade_date, float(b.close), float(b.high), float(b.low),
             float(b.open), int(b.volume or 0)) for b in bars]

    # 新浪实时
    today = None
    try:
        url = f"https://hq.sinajs.cn/list={sina_code}"
        req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read().decode("gbk")
        parts = raw.split('"')[1].split(",")
        if len(parts) >= 9 and parts[3] != "":
            today = ("2026-07-03", float(parts[3]), float(parts[4]),
                     float(parts[5]), float(parts[1]), int(parts[8]))
    except Exception as e:
        print(f"  (实时数据获取失败: {e})")

    if today:
        # 避免重复
        if today[0] not in {d[0] for d in data}:
            data.append(today)
            data.sort(key=lambda x: x[0])

    # 用最近 80 天
    recent = data[-80:]
    n = len(recent)
    closes = [d[1] for d in recent]
    highs = [d[2] for d in recent]
    lows = [d[3] for d in recent]
    volumes = [d[5] for d in recent]
    dates = [d[0] for d in recent]

    corr = rolling_corr(highs, volumes, 10)

    daily_vol = [0.0]
    for i in range(1, n):
        chg = abs(closes[i]/closes[i-1]-1) if closes[i-1] else 0
        daily_vol.append(chg)
    vs = sma(daily_vol, 10)
    vl = sma(daily_vol, 60)
    vol_amp = [None]*n
    for i in range(n):
        if vs[i] and vl[i] and vl[i]>0.0001:
            vol_amp[i] = vs[i]/vl[i]

    h20 = rolling_max(highs, 20)

    chg5 = [None]*n
    for i in range(n):
        if i>=5 and closes[i-5]>0:
            chg5[i] = (closes[i]-closes[i-5])/closes[i-5]

    # MA
    ma5 = sum(closes[-5:])/5 if n>=5 else 0
    ma10 = sum(closes[-10:])/10 if n>=10 else 0
    ma20 = sum(closes[-20:])/20 if n>=20 else 0

    # 今天数据
    price_today = closes[-1]
    is_today = dates[-1] == "2026-07-03"
    chg_today = (closes[-1]/closes[-2]-1)*100 if n>=2 and closes[-2] else 0

    # 最近 corr
    latest_corr = None
    for i in range(n-1, -1, -1):
        if corr[i] is not None:
            latest_corr = corr[i]
            break

    # 输出概要卡
    print(f"\n  {'今日' if is_today else '最新'}: {dates[-1]} | 收盘: {price_today:.2f} ({chg_today:+.2f}%)")
    print(f"  MA5: {ma5:.2f} | MA10: {ma10:.2f} | MA20: {ma20:.2f}")
    print(f"  corr(high,vol,10): {latest_corr:.3f}" if latest_corr else "")
    print(f"  波动放大: {vol_amp[-1]:.1f}x" if vol_amp[-1] else "")
    print(f"  距20日高: {(price_today/h20[-1]-1)*100:+.1f}%" if h20[-1] else "")
    print(f"  5日涨跌: {chg5[-1]*100:+.1f}%" if chg5[-1] else "")

    # 详细表
    print(f"\n  {'日期':12s} {'收盘':>8s} {'涨幅':>8s} {'corr':>8s} {'波放':>5s} {'距20高':>7s} {'5日%':>7s} {'量(万手)':>9s} {'信号'}")
    print("  " + "-" * 80)

    for i in range(max(0, n-15), n):
        c = closes[i]; h = highs[i]; v = volumes[i]
        cv = corr[i]; va = vol_amp[i]; hh = h20[i]; c5 = chg5[i]
        chg_str = f"{(c/closes[i-1]-1)*100:+.2f}%" if i>0 and closes[i-1]>0 else ""
        cv_str = f"{cv:.3f}" if cv is not None else "-"
        va_str = f"{va:.1f}" if va is not None else "-"
        hh_str = f"{(c/hh-1)*100:+.1f}%" if hh is not None else "-"
        c5_str = f"{c5*100:+.1f}%" if c5 is not None else "-"
        v_str = f"{v/10000:.0f}"
        sig = ""
        if cv is not None:
            if cv > 0.50: sig = ">> [卖出!!]"
            elif cv > 0.30: sig = "> [趋同]"
            elif cv < -0.25: sig = "<< [背离]"
        mark = " <--" if dates[i] == "2026-07-03" else ""
        print(f"  {dates[i]} {c:>8.2f} {chg_str:>8s} {cv_str:>8s} {va_str:>5s} {hh_str:>7s} {c5_str:>7s} {v_str:>9s} {sig}{mark}")

    # corr 趋势
    print(f"\n  corr 7日趋势:")
    rc = [(dates[i], corr[i]) for i in range(n) if corr[i] is not None]
    for i in range(max(0, len(rc)-7), len(rc)):
        arrow = "▲" if rc[i][1] > 0 else "▼"
        print(f"    {rc[i][0]}: {rc[i][1]:+.3f} {arrow}")

    # 结论
    print()
    if latest_corr is not None:
        if latest_corr > 0.50:
            print(f"  [结论] corr={latest_corr:.3f} > 0.50 -> 量价同步，卖出信号 -> 跑！")
        elif latest_corr > 0.30:
            print(f"  [结论] corr={latest_corr:.3f} 偏高，量价趋同 -> 减仓/观望")
        elif latest_corr < -0.25:
            above_ma5 = price_today > ma5
            near_h = h20[-1] and (price_today/h20[-1]) > 0.9
            if above_ma5 and near_h:
                print(f"  [结论] corr={latest_corr:.3f} 缩量背离+强势位置 -> 持有")
            else:
                print(f"  [结论] corr={latest_corr:.3f} 缩量背离但位置走弱 -> 持有设止损")
        else:
            if price_today < ma5:
                print(f"  [结论] corr={latest_corr:.3f} 中性+破MA5 -> 观望/减仓")
            else:
                print(f"  [结论] corr={latest_corr:.3f} 中性 -> 观望")

sess.close()
