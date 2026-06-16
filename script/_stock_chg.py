import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
s = Session()

codes = ['sz.300160','sz.300341','sz.002403','sz.000785','sh.603370']
names = {'sz.300160':'秀强股份','sz.300341':'麦克奥迪','sz.002403':'爱仕达','sz.000785':'居然智家','sh.603370':'华新精科'}

latest = s.query(func.max(StockDaily.trade_date)).scalar()
print(f'最新数据日期: {latest}')
print()

# Target dates: ~1mo, ~6mo, ~1yr ago
# Find closest trading dates
for code in codes:
    bars = s.query(StockDaily).filter(StockDaily.code == code).order_by(StockDaily.trade_date).all()
    if len(bars) < 2:
        continue

    name = names[code]
    now_close = bars[-1].close
    now_date = bars[-1].trade_date

    # Find closest date to N months ago
    results = {}
    for months, label in [(1, '1个月'), (3, '3个月'), (6, '半年'), (12, '1年')]:
        target = len(bars) - 1
        # Walk back roughly months*20 trading days
        target_idx = max(0, len(bars) - 1 - months * 21)
        # Find the actual closest bar
        past_bar = bars[target_idx]
        past_close = past_bar.close
        chg = (now_close / past_close - 1) * 100
        results[label] = (past_bar.trade_date, chg)

    # YTD (2026-01-01)
    ytd_bar = None
    for b in bars:
        if b.trade_date >= '2026-01-01':
            ytd_bar = b
            break
    ytd_chg = (now_close / ytd_bar.close - 1) * 100 if ytd_bar else 0

    print(f'{code} {name}')
    print(f'  最新价: {now_close:.2f} ({now_date})')
    if ytd_bar:
        print(f'  今年来(YTD): {ytd_chg:+.2f}%')
    for label, (date, chg) in results.items():
        print(f'  {label} ({date}): {chg:+.2f}%')
    print()

s.close()
