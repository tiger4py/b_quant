import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
s = Session()

codes = ['sz.300160','sz.300341','sz.002403','sz.000785','sh.603370']
for code in codes:
    info = s.query(StockInfo).filter(StockInfo.code == code).first()
    bars = s.query(StockDaily).filter(StockDaily.code == code).order_by(StockDaily.trade_date.desc()).limit(10).all()
    bars = list(reversed(bars))
    if info:
        print(f'=== {code} {info.name} ===')
        circ = info.circ_shares or 0
        print(f'  市场: {info.market} | 上市: {info.ipo_date} | 流通股本: {circ/10000:.0f}万股')
        if bars:
            for b in bars[-5:]:
                chg = (b.close/b.open-1)*100 if b.open else 0
                print(f'  {b.trade_date} O:{b.open:.2f} C:{b.close:.2f} chg:{chg:+.2f}% 换手:{b.turn:.1f}% 成交:{b.amount/1e8:.2f}亿')
            if len(bars) >= 5:
                chg5 = (bars[-1].close/bars[-5].close-1)*100
                print(f'  5日净涨跌: {chg5:+.2f}%')
        print()
s.close()
