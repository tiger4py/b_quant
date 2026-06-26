"""并行测试早期退出参数 — 4进程并发"""
import sys, os, re, subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SFILE = os.path.join(ROOT, 'backtest/strategy/strategy_trend_following.py')

with open(SFILE, 'r', encoding='utf-8') as f:
    ORIG = f.read()

combos = [(d, th) for d in [3,4,5] for th in [-1.5, -2, -2.5, -3, -3.5, -4]] + [(2, -1.5), (2, -2)]
# 20 combos

def run_one(args):
    day, thresh = args
    content = ORIG
    content = re.sub(r'elif hold_days >= \d+ and profit_pct <= -?\d+\.?\d*:',
                     f'elif hold_days >= {day} and profit_pct <= {thresh}:', content)

    tmp_file = os.path.join(ROOT, f'backtest/strategy/_tmp_{day}_{thresh}.py')
    with open(tmp_file, 'w', encoding='utf-8') as f:
        f.write(content)

    # 直接用 python 跑，不通过 subprocess 调 runner（避免文件冲突）
    proc = subprocess.run([sys.executable, '-c', f'''
import sys
sys.path.insert(0, r"{ROOT}")
from backtest.portfolio import run_portfolio_backtest
import importlib.util, json, os

spec = importlib.util.spec_from_file_location("strat", r"{tmp_file}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from logic.backtest_cache import load_market_bars
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL

engine = create_engine(DATABASE_URL, echo=False)
sess = sessionmaker(bind=engine)()
stocks, bars_by_code, latest = load_market_bars(sess, days=1000)
sess.close()
stock_map = {{s["code"]: s for s in stocks}}

bt = run_portfolio_backtest(bars_by_code, stock_map, mod, initial_cash=1000000.0, max_positions=5)
s = bt["summary"]
print(f"{{s['"'"'total_return_pct'"'"']}}|{{s['"'"'max_drawdown_pct'"'"']}}|{{s['"'"'win_rate_pct'"'"']}}|{{s['"'"'trade_count'"'"']}}|{{s['"'"'profit_factor'"'"']}}")
'''], cwd=ROOT, capture_output=True, text=True)

    os.remove(tmp_file)
    # also remove __pycache__
    import shutil
    cache_dir = os.path.join(os.path.dirname(tmp_file), '__pycache__')
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)

    out = proc.stdout.strip()
    parts = out.split('|')
    if len(parts) >= 5:
        return {'day': day, 'thresh': thresh, 'ret': float(parts[0]),
                'dd': float(parts[1]), 'wr': float(parts[2]),
                'trades': int(parts[3]), 'pf': float(parts[4])}
    return {'day': day, 'thresh': thresh, 'ret': None, 'error': out}

print(f'并行测试 {len(combos)} 组 (4 workers)...')
results = []
with ProcessPoolExecutor(max_workers=4) as ex:
    futures = {ex.submit(run_one, c): c for c in combos}
    for i, f in enumerate(as_completed(futures)):
        r = f.result()
        results.append(r)
        if r['ret'] is not None:
            print(f'[{i+1}/{len(combos)}] d={r["day"]} th={r["thresh"]:+.1f}% -> ret={r["ret"]:+.2f}% dd={r["dd"]:.1f}% wr={r["wr"]:.1f}% tr={r["trades"]} pf={r["pf"]:.2f}')
        else:
            print(f'[{i+1}/{len(combos)}] d={r["day"]} th={r["thresh"]:+.1f}% -> ERROR')

results.sort(key=lambda x: -(x['ret'] or -999))
print(f'\n===== 结果排序 =====')
print(f'{"天数":>4} {"阈值":>7} {"收益%":>8} {"回撤%":>8} {"胜率%":>8} {"交易":>5} {"盈亏比":>6}')
for r in results:
    if r['ret'] is not None:
        print(f'{r["day"]:>4} {r["thresh"]:>+6.1f}% {r["ret"]:>+8.2f}% {r["dd"]:>8.2f}% {r["wr"]:>8.2f}% {r["trades"]:>5} {r["pf"]:>6.2f}')

# 恢复
with open(SFILE, 'w', encoding='utf-8') as f:
    f.write(ORIG)
