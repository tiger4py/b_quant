"""分析止损组和到期组的早期表现：能否提前退出"""
import json, sys, os
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily

with open('data/strategy/trend_following/2026-06/2026-06-23_21.json', encoding='utf-8') as f:
    d = json.load(f)

sl = [t for t in d['trades'] if '止损' in t.get('sell_reason','') and 'ATR' not in t['sell_reason']]
mature = [t for t in d['trades'] if '天到期' in t.get('sell_reason','')]
tp = [t for t in d['trades'] if '止盈' in t.get('sell_reason','')]

print(f'止损{len(sl)}笔  到期{len(mature)}笔  止盈{len(tp)}笔\n')

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

def get_prices_after(code, buy_date, days_list):
    """获取买入后第N天的价格"""
    try:
        bd = datetime.strptime(buy_date, '%Y-%m-%d')
    except:
        return {}
    result = {}
    for d in days_list:
        t = bd + timedelta(days=d)
        for o in range(4):
            nd = t + timedelta(days=o)
            r = sess.query(StockDaily).filter(StockDaily.code == code, StockDaily.trade_date == nd.strftime('%Y-%m-%d')).first()
            if r and r.close:
                result[d] = float(r.close)
                break
    return result

def analyze_group(trades, label, days_check=[1,2,3,4,5,8,12]):
    """分析一组交易在买入后前几天的表现"""
    results = {d: [] for d in days_check}
    results['max_dd'] = []  # 最大回撤（从买入到卖出）
    results['first_red'] = []  # 第一天就跌的
    results['never_green'] = []  # 从未回到买入价以上

    for t in trades:
        buy_price = t['buy_price']
        buy_date = t['buy_date']
        prices = get_prices_after(t['code'], buy_date, days_check)

        for d in days_check:
            if d in prices:
                chg = (prices[d] / buy_price - 1) * 100
                results[d].append(chg)

    print(f'\n=== {label} ({len(trades)}笔) ===')
    print(f'{"买入后天数":>10} {"均值%":>8} {"中位数%":>8} {"上涨占比":>8} {"<-3%占比":>8} {"<-5%占比":>8}')
    for d in days_check:
        chgs = results[d]
        if not chgs: continue
        up = sum(1 for c in chgs if c > 0)
        down3 = sum(1 for c in chgs if c < -3)
        down5 = sum(1 for c in chgs if c < -5)
        print(f'{"第"+str(d)+"天":>10} {sum(chgs)/len(chgs):>+7.2f}% {sorted(chgs)[len(chgs)//2]:>+7.2f}% {up/len(chgs)*100:>7.1f}% {down3/len(chgs)*100:>7.1f}% {down5/len(chgs)*100:>7.1f}%')

    return results

r_sl = analyze_group(sl, '硬止损(-8%)')
r_mature = analyze_group(mature, '持仓15天到期')
r_tp = analyze_group(tp, '止盈(+25%)')

# 关键对比：第3天表现
print(f'\n===== 第3天对比 =====')
for label, data in [('止损组', r_sl), ('到期组', r_mature), ('止盈组', r_tp)]:
    chgs = data.get(3, [])
    if chgs:
        up = sum(1 for c in chgs if c > 0)
        down2 = sum(1 for c in chgs if c < -2)
        print(f'{label}: 均值{sum(chgs)/len(chgs):+.2f}%  上涨{up}/{len(chgs)}({up/len(chgs)*100:.0f}%)  <-2%: {down2}/{len(chgs)}({down2/len(chgs)*100:.0f}%)')

# 止损组: 第1天就跌的占多少
print(f'\n===== 早期预警信号 =====')
sl_day1_chgs = r_sl.get(1, [])
if sl_day1_chgs:
    day1_down = sum(1 for c in sl_day1_chgs if c < 0)
    day1_down2 = sum(1 for c in sl_day1_chgs if c < -2)
    print(f'止损组-第1天就跌: {day1_down}/{len(sl_day1_chgs)} ({day1_down/len(sl_day1_chgs)*100:.0f}%)')
    print(f'止损组-第1天跌超2%: {day1_down2}/{len(sl_day1_chgs)} ({day1_down2/len(sl_day1_chgs)*100:.0f}%)')

    # 止损组中,第1天跌的vs第1天涨的,最终亏损差异
    day1_up = [c for c in sl_day1_chgs if c > 0]
    day1_dn = [c for c in sl_day1_chgs if c < 0]
    if day1_up and day1_dn:
        print(f'  第1天涨的: 占比{len(day1_up)/len(sl_day1_chgs)*100:.0f}%')
        print(f'  第1天跌的: 占比{len(day1_dn)/len(sl_day1_chgs)*100:.0f}%')

sess.close()
