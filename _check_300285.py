"""查看国瓷材料 300285"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

code = "sz.300285"

# 基本信息
stock = sess.query(StockInfo).filter(StockInfo.code == code).first()
if not stock:
    print("数据库中未找到 300285")
    sess.close()
    exit()

print(f"股票: {stock.code} {stock.name} 市场:{stock.market}")

# 最近K线
bars = sess.query(StockDaily).filter(
    StockDaily.code == code
).order_by(StockDaily.trade_date.desc()).limit(30).all()

bars = list(reversed(bars))

if bars:
    print(f"\n最近 {len(bars)} 天K线:")
    print(f"{'日期':12s} {'开盘':>7s} {'收盘':>7s} {'最高':>7s} {'最低':>7s} {'涨幅':>8s} {'成交额(万)':>10s}")

    # 计算 MA5, MA10, MA20, MA60
    closes = [b.close for b in bars]
    for i, b in enumerate(bars):
        chg = ""
        if i > 0 and bars[i-1].close > 0:
            chg_pct = (b.close - bars[i-1].close) / bars[i-1].close * 100
            chg = f"{chg_pct:+.2f}%"
        amt = (b.amount or 0) / 10000
        print(f"{b.trade_date} {b.open:>7.2f} {b.close:>7.2f} {b.high:>7.2f} {b.low:>7.2f} {chg:>8s} {amt:>10.0f}")

    # 均线
    print(f"\n均线:")
    for label, period in [("MA5", 5), ("MA10", 10), ("MA20", 20), ("MA60", 60)]:
        if len(closes) >= period:
            ma = sum(closes[-period:]) / period
            print(f"  {label}: {ma:.2f}  (当前价 {closes[-1]:.2f}, {'上方' if closes[-1] > ma else '下方'})")
        else:
            print(f"  {label}: 数据不足")

    # 最近涨跌
    if len(closes) >= 6:
        chg5 = (closes[-1] / closes[-6] - 1) * 100
        print(f"  5日涨跌: {chg5:+.2f}%")
    if len(closes) >= 21:
        chg20 = (closes[-1] / closes[-21] - 1) * 100
        print(f"  20日涨跌: {chg20:+.2f}%")

    # 量能
    volumes = [b.volume or 0 for b in bars]
    if len(volumes) >= 5:
        avg_vol_5 = sum(volumes[-5:]) / 5
        avg_vol_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else avg_vol_5
        vol_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 0
        print(f"\n量能: 5日均量/20日均量 = {vol_ratio:.2f}x")

sess.close()
