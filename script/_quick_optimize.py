"""快速参数寻优：200只股票粗筛 → 全量验证Top5"""
import sys, os, time, itertools, json, random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from logic.backtest_cache import load_market_bars
from backtest.portfolio import run_portfolio_backtest
from backtest.strategy import strategy_trend_following as strat

# ====== 加载全量 ======
print('[1/3] Loading data...', flush=True)
t0 = time.time()
engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()

stocks_all, bars_all, latest = load_market_bars(sess, days=1000)
sess.close()

stock_map_all = {s['code']: s for s in stocks_all}
print(f'     {len(stocks_all)} stocks, {time.time()-t0:.0f}s', flush=True)

# ====== 随机抽200只 ======
random.seed(42)
sample_codes = set(random.sample(list(bars_all.keys()), 200))
bars_sample = {c: bars_all[c] for c in sample_codes}
stock_map_sample = {c: stock_map_all[c] for c in sample_codes}

# ====== 参数网格 ======
combo = list(itertools.product(
    [10, 12, 14, 16, 18, 20],        # hold_days
    [-4, -6, -8, -10, -12],           # stop_loss
    [15, 18, 20, 22, 25, 28, 30, 35],# take_profit
    [-4, -6, -8, -10],                # stop_daily
))
valid = [(h, sl, tp, sld) for h, sl, tp, sld in combo if tp > abs(sl)]
print(f'[2/3] Screening {len(valid)} combos on 200 stocks...', flush=True)

# ====== 粗筛 ======
results = []
t0 = time.time()
for idx, (h, sl, tp, sld) in enumerate(valid):
    strat.MAX_HOLD_DAYS = h
    strat.STOP_LOSS_PCT = sl
    strat.TAKE_PROFIT_PCT = tp
    strat.DAILY_CRASH_PCT = sld

    try:
        bt = run_portfolio_backtest(bars_sample, stock_map_sample, strat,
                                     initial_cash=1000000.0, max_positions=5)
        ret = bt['summary']['total_return_pct']
        dd = bt['summary']['max_drawdown_pct']
    except:
        continue

    results.append({'h': h, 'sl': sl, 'tp': tp, 'sld': sld, 'ret': ret, 'dd': dd})

    if (idx+1) % 10 == 0:
        elapsed = time.time() - t0
        eta = elapsed/(idx+1)*(len(valid)-idx-1)
        print(f'     [{idx+1}/{len(valid)}] {elapsed:.0f}s ETA {eta:.0f}s', flush=True)

results.sort(key=lambda x: -x['ret'])
print(f'     Done in {time.time()-t0:.0f}s', flush=True)

# Top 10
print(f'\n     Top 10 (200 stocks):')
for i, r in enumerate(results[:10]):
    print(f'     #{i+1} ret={r["ret"]:+.1f}% dd={r["dd"]:.1f}% h={r["h"]} sl={r["sl"]} tp={r["tp"]} sld={r["sld"]}')

# ====== 全量验证 Top 5 ======
print(f'\n[3/3] Verifying top 5 on all {len(stocks_all)} stocks...', flush=True)
for i, r in enumerate(results[:5]):
    strat.MAX_HOLD_DAYS = r['h']
    strat.STOP_LOSS_PCT = r['sl']
    strat.TAKE_PROFIT_PCT = r['tp']
    strat.DAILY_CRASH_PCT = r['sld']

    t1 = time.time()
    bt = run_portfolio_backtest(bars_all, stock_map_all, strat,
                                 initial_cash=1000000.0, max_positions=5)
    s = bt['summary']
    r['full_ret'] = s['total_return_pct']
    r['full_dd'] = s['max_drawdown_pct']
    r['full_wr'] = s['win_rate_pct']
    r['full_tr'] = s['trade_count']
    r['full_pf'] = s['profit_factor']
    print(f'     #{i+1} h={r["h"]} sl={r["sl"]} tp={r["tp"]} sld={r["sld"]} '
          f'-> full_ret={r["full_ret"]:+.1f}% dd={r["full_dd"]:.1f}% '
          f'wr={r["full_wr"]:.1f}% tr={r["full_tr"]} pf={r["full_pf"]:.2f} '
          f'({time.time()-t1:.0f}s)', flush=True)

# 恢复
strat.MAX_HOLD_DAYS = 15
strat.STOP_LOSS_PCT = -8
strat.TAKE_PROFIT_PCT = 25
strat.DAILY_CRASH_PCT = -8

# ====== 最终输出 ======
print(f'\n===== OPTIMAL =====')
best = max([r for r in results if 'full_ret' in r], key=lambda x: x['full_ret'])
print(f'hold_days={best["h"]}')
print(f'stop_loss_total={best["sl"]}')
print(f'take_profit={best["tp"]}')
print(f'stop_loss_daily={best["sld"]}')
print(f'return={best["full_ret"]:+.1f}% drawdown={best["full_dd"]:.1f}%')
print(f'win_rate={best["full_wr"]:.1f}% trades={best["full_tr"]} pf={best["full_pf"]:.2f}')

# 保存
out = os.path.join(ROOT, 'data/strategy/trend_following/optimize_result.json')
with open(out, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f'\nSaved: {out}')
