"""主流ETF测试 + 组合模拟"""
import sys; sys.path.insert(0, '.')
import akshare as ak
from backtest.strategy.strategy_alpha042 import *
from backtest.indicators import sma

# 精选70只主流ETF
ETF_LIST = [
    # 宽基
    ('510050','上证50'),('510300','沪深300'),('510500','中证500'),('510880','红利ETF'),
    ('159915','创业板'),('159949','创业板50'),('588000','科创50'),('512100','中证1000'),
    ('510210','综指ETF'),('510180','180ETF'),('159845','中证1000ETF'),('588080','科创板50'),
    # 行业ETF
    ('512880','证券ETF'),('512800','银行ETF'),('512660','军工ETF'),('512690','酒ETF'),
    ('512010','医药ETF'),('512170','医疗ETF'),('512760','芯片ETF'),('512480','半导体ETF'),
    ('515050','5GETF'),('515790','光伏ETF'),('516160','新能源ETF'),('159995','芯片ETF'),
    ('512980','传媒ETF'),('510230','金融ETF'),('512070','非银ETF'),('512200','地产ETF'),
    ('515220','煤炭ETF'),('516880','电力ETF'),('159766','旅游ETF'),('516510','云计算ETF'),
    ('515030','新汽车ETF'),('159806','新能源车ETF'),('512720','计算机ETF'),('515210','钢铁ETF'),
    ('159865','养殖ETF'),('159825','农业ETF'),('516780','稀土ETF'),('159605','中概互联ETF'),
    # 策略/主题
    ('510050','上证50'),('563000','中证2000'),('159629','1000增强'),('515800','800ETF'),
    ('588200','科创芯片'),('562800','稀有金属'),('159869','游戏ETF'),('516820','医疗创新'),
    ('159865','畜牧ETF'),('512580','环保ETF'),('515890','红利低波'),('562880','电池ETF'),
    # 商品/跨境
    ('159937','黄金ETF'),('513100','纳指100'),('513050','中概互联'),('510900','H股ETF'),
    ('159866','日经ETF'),('513500','标普500'),('159570','港股通100'),('159699','恒生消费'),
]

results = []
all_equity = []  # 用于组合模拟

for idx, (code, name) in enumerate(ETF_LIST):
    try:
        df = ak.fund_etf_hist_em(symbol=code, period='daily', start_date='20220101', end_date='20260702', adjust='qfq')
        df = df.sort_values('日期')
        closes = df['收盘'].tolist(); highs = df['最高'].tolist()
        volumes = df['成交量'].tolist()
        n = len(closes)
        if n < 200: continue

        dc=[0.0]; dv=[0.0]
        for i in range(1,n): ch=closes[i]/closes[i-1]-1 if closes[i-1] else 0; dc.append(ch); dv.append(abs(ch))
        vs=sma(dv,VOL_SHORT); vl=sma(dv,VOL_LONG)
        corr=_rolling_corr(highs,volumes,10); h20=_rolling_max(highs,PRICE_NEAR_HIGH_LOOKBACK)

        cash=1_000_000; shares=0; ep=0; ei=0; trades=0; wins=0
        equity_curve=[]
        min_idx=65
        for i in range(min_idx,n):
            close=closes[i]; cv=corr[i]; hh=h20[i]
            if cv is None or hh is None: continue
            vss=vs[i]; vll=vl[i]
            if vss is None or vll is None or vll<0.0001: continue
            va=vss/vll
            if i<5 or closes[i-5]<=0: continue
            c5d=(close-closes[i-5])/closes[i-5]

            if shares==0:
                if cv<CORR_BUY_MAX and VOL_AMP_MIN<=va<=VOL_AMP_MAX and close>=hh*(1-PRICE_NEAR_HIGH_PCT) and c5d>CHG_5D_MIN:
                    shares=int(cash//close//100*100)
                    if shares>0: cash-=shares*close; ep=close; ei=i; trades+=1
            else:
                if (cv is not None and cv>CORR_SELL_THRESH) or (i-ei)>=MAX_HOLD_DAYS:
                    pnl=(close-ep)*shares; cash+=shares*close
                    if pnl>0: wins+=1
                    shares=0; ep=0

        final=cash+shares*closes[-1] if shares else cash
        ret=(final/1_000_000-1)*100
        if trades>=2:
            wr=wins/max(1,trades)*100
            results.append((code,name,ret,trades,wr))
            if len(all_equity)==0: all_equity=equity_curve
        if (idx+1)%20==0: print(f'  进度:{idx+1}/{len(ETF_LIST)}')
    except Exception as e:
        pass

results.sort(key=lambda x:-x[2])
print(f'\n有效结果: {len(results)}只')
print(f'{"代码":<10} {"名称":<14} {"收益":>7} {"交易":>5} {"胜率":>6}')
print('-'*46)
for code,name,ret,trades,wr in results:
    print(f'{code:<10} {name:<14} {ret:>+6.1f}% {trades:>4}笔 {wr:>5.0f}%')

# 统计
positive = sum(1 for r in results if r[2]>0)
avg_ret = sum(r[2] for r in results)/len(results)
avg_trades = sum(r[3] for r in results)/len(results)
print(f'\n正收益: {positive}/{len(results)} ({positive/len(results)*100:.0f}%)')
print(f'平均收益: {avg_ret:+.1f}%  平均交易: {avg_trades:.1f}笔')
print(f'收益>30%: {sum(1 for r in results if r[2]>30)}只')
print(f'收益>50%: {sum(1 for r in results if r[2]>50)}只')
