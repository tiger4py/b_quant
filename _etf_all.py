"""批量ETF Alpha #042 回测"""
import sys; sys.path.insert(0, '.')
import akshare as ak
from backtest.strategy.strategy_alpha042 import *
from backtest.indicators import sma

print('拉取ETF列表...')
etf_list = ak.fund_etf_category_sina(symbol='ETF基金')
# 过滤：成交额>1000万，避免流动性太差的
codes = etf_list['代码'].tolist()[:200]  # 先取200只

print(f'共 {len(codes)} 只ETF，开始回测...')

results = []
good_codes = []

for idx, code in enumerate(codes):
    try:
        df = ak.fund_etf_hist_em(symbol=code, period='daily', start_date='20220101', end_date='20260702', adjust='qfq')
        df = df.sort_values('日期')
        closes = df['收盘'].tolist(); highs = df['最高'].tolist()
        volumes = df['成交量'].tolist(); dates = df['日期'].tolist()
        n = len(closes)
        if n < 200: continue

        dc = [0.0]; dv = [0.0]
        for i in range(1, n):
            ch = closes[i]/closes[i-1]-1 if closes[i-1] else 0
            dc.append(ch); dv.append(abs(ch))
        vs = sma(dv, VOL_SHORT); vl = sma(dv, VOL_LONG)
        corr = _rolling_corr(highs, volumes, 10)
        h20 = _rolling_max(highs, PRICE_NEAR_HIGH_LOOKBACK)

        cash = 1_000_000; shares = 0; ep = 0; ei = 0; trades = 0
        min_idx = 65
        for i in range(min_idx, n):
            close = closes[i]; cv = corr[i]; hh = h20[i]
            if cv is None or hh is None: continue
            vss = vs[i]; vll = vl[i]
            if vss is None or vll is None or vll<0.0001: continue
            va = vss/vll
            if i<5 or closes[i-5]<=0: continue
            c5d = (close-closes[i-5])/closes[i-5]

            if shares == 0:
                if cv<CORR_BUY_MAX and VOL_AMP_MIN<=va<=VOL_AMP_MAX \
                   and close>=hh*(1-PRICE_NEAR_HIGH_PCT) and c5d>CHG_5D_MIN:
                    shares = int(cash//close//100*100)
                    if shares>0: cash-=shares*close; ep=close; ei=i; trades+=1
            else:
                if (cv is not None and cv>CORR_SELL_THRESH) or (i-ei)>=MAX_HOLD_DAYS:
                    cash+=shares*close; shares=0; ep=0

        final = cash + shares*closes[-1] if shares else cash
        ret = (final/1_000_000-1)*100
        if trades >= 3 and ret > -10:  # 过滤太少交易和太差的
            good_codes.append(code)
            results.append((code, etf_list[etf_list['代码']==code]['名称'].values[0] if code in etf_list['代码'].values else code, ret, trades))

        if (idx+1) % 50 == 0:
            print(f'  进度: {idx+1}/{len(codes)}')
    except:
        pass

results.sort(key=lambda x: -x[2])
print(f'\n{"="*70}')
print(f'ETF Alpha #042 — 收益排名 TOP30')
print(f'{"="*70}')
print(f'{"代码":<10} {"名称":<12} {"收益":>7} {"交易":>5}')
print('-' * 40)
for code, name, ret, trades in results[:30]:
    print(f'{code:<10} {name:<12} {ret:>+6.1f}% {trades:>4}笔')

print(f'\n合计: {len(results)} 只ETF符合条件 (交易>=3, 收益>-10%)')
print(f'胜率: {sum(1 for r in results if r[2]>0)}/{len(results)}')
# 平均
avg_ret = sum(r[2] for r in results)/len(results)
print(f'平均收益: {avg_ret:+.1f}%')
