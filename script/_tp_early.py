"""止盈组在前3天有没有跌到会被早期退出的程度"""
import json, sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily

with open('data/strategy/trend_following/2026-06/2026-06-23_21.json', encoding='utf-8') as f:
    d = json.load(f)

tp = [t for t in d['trades'] if '止盈' in t.get('sell_reason','')]
mature = [t for t in d['trades'] if '天到期' in t.get('sell_reason','')]
sl = [t for t in d['trades'] if '止损' in t.get('sell_reason','') and 'ATR' not in t['sell_reason']]

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

def max_drawdown(code, buy_date, buy_price, days):
    """买入后N天内的最大回撤(%)"""
    try:
        bd = datetime.strptime(buy_date, '%Y-%m-%d')
    except:
        return []
    end = bd + timedelta(days=days)
    rows = sess.query(StockDaily).filter(StockDaily.code == code,
        StockDaily.trade_date > buy_date,
        StockDaily.trade_date <= end.strftime('%Y-%m-%d')).all()
    buy_px = float(buy_price)
    daily_chgs = []
    for r in rows:
        daily_chgs.append((float(r.close) / buy_px - 1) * 100)
    return daily_chgs

def check_group(trades, label, rules):
    print(f'\n=== {label} ({len(trades)}笔) ===')
    for rule_name, (min_day, threshold) in rules.items():
        hit = 0
        for t in trades:
            chgs = max_drawdown(t['code'], t['buy_date'], t['buy_price'], min_day+2)
            if chgs and min(chgs) <= threshold:
                hit += 1
        print(f'  如果 d{min_day} -{abs(threshold)}%: {hit}笔 ({hit/len(trades)*100:.0f}%)')

rules = {
    'd2 -2%':  (2, -2),
    'd3 -2%':  (3, -2),
    'd3 -3%':  (3, -3),
    'd4 -3%':  (4, -3),
    'd5 -3%':  (5, -3),
}

check_group(tp, '止盈(+25%)', rules)
check_group(mature, '持仓到期', rules)
check_group(sl, '硬止损(-8%)', rules)

sess.close()
