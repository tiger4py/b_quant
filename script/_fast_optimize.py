"""快速参数寻优 — 加载一次数据，内存中循环跑回测"""
import sys, os, time, itertools, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from logic.backtest_cache import load_market_bars
from backtest.portfolio import run_portfolio_backtest
from backtest.strategy import strategy_trend_following as strat

# ====== 一次性加载全市场数据 ======
print('加载全市场数据...')
t0 = time.time()
engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

stocks, bars_by_code, latest_date = load_market_bars(sess, days=1000)
sess.close()

stock_map = {s['code']: s for s in stocks}
print(f'股票: {len(stocks)}, 加载耗时: {time.time()-t0:.1f}s')
print(f'最新日期: {latest_date}')

# ====== 参数网格 ======
param_grid = {
    'hold_days':       [10, 12, 14, 16, 18, 20],
    'stop_loss_total': [-4, -6, -8, -10],
    'take_profit':     [15, 18, 20, 22, 25, 28, 30],
    'stop_loss_daily': [-4, -6, -8, -10],
}
combos = list(itertools.product(
    param_grid['hold_days'], param_grid['stop_loss_total'],
    param_grid['take_profit'], param_grid['stop_loss_daily'],
))
# 6*4*7*4 = 672 组, 去掉不合理后~500组

valid_combos = [(h, sl, tp, sld) for h, sl, tp, sld in combos if tp > abs(sl) and abs(sld) <= abs(sl)]
print(f'有效组合: {len(valid_combos)} (滤除不合理组合)')

# ====== 跑 ======
results = []
t0 = time.time()
for idx, (hd, sl, tp, sld) in enumerate(valid_combos):
    strat.MAX_HOLD_DAYS = hd
    strat.STOP_LOSS_PCT = sl
    strat.TAKE_PROFIT_PCT = tp
    strat.DAILY_CRASH_PCT = sld

    try:
        bt = run_portfolio_backtest(bars_by_code, stock_map, strat,
                                     initial_cash=1000000.0, max_positions=5)
        ret = bt['summary']['total_return_pct']
        dd = bt['summary']['max_drawdown_pct']
        wr = bt['summary']['win_rate_pct']
        tr = bt['summary']['trade_count']
        pf = bt['summary']['profit_factor']
    except Exception as e:
        continue

    results.append({'hold_days': hd, 'stop_loss': sl, 'take_profit': tp, 'stop_daily': sld,
                    'return': ret, 'drawdown': dd, 'win_rate': wr, 'trades': tr, 'profit_factor': pf})

    if (idx + 1) % 5 == 0 or (idx + 1) == len(valid_combos):
        elapsed = time.time() - t0
        rate = elapsed / (idx + 1)
        eta = rate * (len(valid_combos) - idx - 1)
        best = max(results, key=lambda x: x['return'])
        print(f'[{idx+1}/{len(valid_combos)}] {elapsed:.0f}s | best: {best["return"]:+.1f}% '
              f'h={best["hold_days"]} sl={best["stop_loss"]} tp={best["take_profit"]} sld={best["stop_daily"]} '
              f'| ETA {eta:.0f}s')

# 恢复
strat.MAX_HOLD_DAYS = 15
strat.STOP_LOSS_PCT = -8
strat.TAKE_PROFIT_PCT = 25
strat.DAILY_CRASH_PCT = -8

# ====== 排序输出 ======
results.sort(key=lambda x: -x['return'])
print(f'\n===== Top 20 =====')
print(f'{"#":>4} {"收益%":>8} {"回撤%":>8} {"胜率%":>8} {"笔数":>5} {"盈亏比":>6} {"h":>4} {"sl":>5} {"tp":>5} {"sld":>5}')
print('-' * 70)
for i, r in enumerate(results[:20]):
    print(f'{i+1:>4} {r["return"]:>+8.1f} {r["drawdown"]:>8.1f} {r["win_rate"]:>8.1f} '
          f'{r["trades"]:>5} {r["profit_factor"]:>6.2f} {r["hold_days"]:>4} {r["stop_loss"]:>5} {r["take_profit"]:>5} {r["stop_daily"]:>5}')

# 保存
out_path = os.path.join(ROOT, 'data/strategy/trend_following/optimize_result.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f'\n结果已保存: {out_path}')
