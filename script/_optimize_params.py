"""sklearn 参数寻优: hold_days / stop_loss_total / take_profit / stop_loss_daily"""
import subprocess, re, sys, os, json, random
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SFILE = os.path.join(ROOT, 'backtest/strategy/strategy_trend_following.py')
RESULT_DIR = os.path.join(ROOT, 'data/strategy/trend_following/2026-06')

with open(SFILE, 'r', encoding='utf-8') as f:
    ORIGINAL = f.read()

# ====== 参数空间 ======
PARAM_SPACE = {
    'hold_days':       list(range(10, 31, 2)),      # 10,12,14,...,30
    'stop_loss_total': [-4, -6, -8, -10, -12, -15], # 百分比, 代码里直接和profit_pct(%)比较
    'take_profit':     [15, 20, 25, 30, 35, 40],    # 百分比
    'stop_loss_daily': [-4, -6, -8, -10, -12],       # 百分比, 代码里 /100 转小数
}

# ====== 随机采样 50 组 ======
N_SAMPLES = 50
random.seed(42)
samples = []
for _ in range(N_SAMPLES):
    samples.append({
        'hold_days':       random.choice(PARAM_SPACE['hold_days']),
        'stop_loss_total': random.choice(PARAM_SPACE['stop_loss_total']),
        'take_profit':     random.choice(PARAM_SPACE['take_profit']),
        'stop_loss_daily': random.choice(PARAM_SPACE['stop_loss_daily']),
    })

# 确保包含当前最优解
samples.append({'hold_days': 15, 'stop_loss_total': -8, 'take_profit': 25, 'stop_loss_daily': -8})

print(f'总样本: {len(samples)} 组')
print()

def run_one(params):
    """跑一次回测，返回 (return%, drawdown%, win_rate%, trades, profit_factor)"""
    content = ORIGINAL
    content = re.sub(r'MAX_HOLD_DAYS = \d+', f'MAX_HOLD_DAYS = {params["hold_days"]}', content)
    content = re.sub(r'STOP_LOSS_PCT = -?\d+\.?\d*', f'STOP_LOSS_PCT = {params["stop_loss_total"]}', content)
    content = re.sub(r'TAKE_PROFIT_PCT = \d+\.?\d*', f'TAKE_PROFIT_PCT = {params["take_profit"]}', content)
    content = re.sub(r'DAILY_CRASH_PCT = -?\d+\.?\d*', f'DAILY_CRASH_PCT = {params["stop_loss_daily"]}', content)

    with open(SFILE, 'w', encoding='utf-8') as f:
        f.write(content)

    cmd = [sys.executable, os.path.join(ROOT, 'script/run_strategy_market_backtest.py'),
           '--strategy', 'trend_following', '--max-positions', '5']
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = proc.stdout + proc.stderr

    def find(p, d=None):
        m = re.search(p, out)
        return float(m.group(1)) if m else d

    return {
        'return': find(r'return=([\d.+-]+)%', -999),
        'drawdown': find(r'drawdown=([\d.+-]+)%', 999),
        'win_rate': find(r'win_rate=([\d.+-]+)%', 0),
        'trades': int(find(r'trades=(\d+)', 0)),
        'profit_factor': find(r'profit_factor=([\d.+-]+)', 0),
    }

# ====== 跑回测 ======
data = []
for idx, params in enumerate(samples):
    print(f'[{idx+1}/{len(samples)}] h={params["hold_days"]:>2}d sl={params["stop_loss_total"]:>5.2f} tp={params["take_profit"]:>.2f} sld={params["stop_loss_daily"]:>5.2f}', end=' ')
    result = run_one(params)
    result.update(params)
    data.append(result)
    print(f'-> ret={result["return"]:>+7.2f}% dd={result["drawdown"]:>6.2f}% wr={result["win_rate"]:>5.1f}% tr={result["trades"]:>4} pf={result["profit_factor"]:.2f}')

# 恢复原始文件
with open(SFILE, 'w', encoding='utf-8') as f:
    f.write(ORIGINAL)

# ====== 保存原始数据 ======
with open(os.path.join(ROOT, 'data/strategy/trend_following/optimization_results.json'), 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f'\n=== 数据已保存，共 {len(data)} 组 ===')

# ====== sklearn 建模 & 寻优 ======
print('\n训练模型...')
valid = [d for d in data if d['return'] > -900]
print(f'有效样本: {len(valid)}')

X = np.array([[d['hold_days'], d['stop_loss_total'], d['take_profit'], d['stop_loss_daily']] for d in valid])
y = np.array([d['return'] for d in valid])

from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.model_selection import cross_val_score

# 两个模型取平均
rf = RandomForestRegressor(n_estimators=200, max_depth=5, random_state=42)
gb = GradientBoostingRegressor(n_estimators=200, max_depth=4, random_state=42)

rf.fit(X, y)
gb.fit(X, y)

# CV 评估
rf_cv = cross_val_score(rf, X, y, cv=5, scoring='neg_mean_absolute_error')
gb_cv = cross_val_score(gb, X, y, cv=5, scoring='neg_mean_absolute_error')
print(f'RF  CV MAE: {-rf_cv.mean():.2f}%')
print(f'GB  CV MAE: {-gb_cv.mean():.2f}%')

# ====== 网格搜索最优解 ======
print('\n网格搜索最优解...')
grid_hold = range(8, 35, 1)
grid_sl = np.arange(-20, -3, 0.5)      # -20% ~ -3%
grid_tp = np.arange(10, 45, 0.5)       # 10% ~ 45%
grid_sld = np.arange(-15, -3, 0.5)     # -15% ~ -3%

best_pred = -999
best_params = None
top_results = []

for h in grid_hold:
    for sl in grid_sl:
        for tp in grid_tp:
            for sld in grid_sld:
                # 排除不合理的组合：止盈 <= 止损绝对值
                if tp <= abs(sl):
                    continue
                x = np.array([[h, sl, tp, sld]])
                pred = (rf.predict(x)[0] + gb.predict(x)[0]) / 2
                if pred > best_pred:
                    best_pred = pred
                    best_params = {'hold_days': h, 'stop_loss_total': round(sl, 3),
                                   'take_profit': round(tp, 3), 'stop_loss_daily': round(sld, 3)}
                top_results.append((pred, h, sl, tp, sld))

top_results.sort(key=lambda x: -x[0])
print(f'\n模型预测最优:')
print(f'  hold_days={best_params["hold_days"]}')
print(f'  stop_loss_total={best_params["stop_loss_total"]}')
print(f'  take_profit={best_params["take_profit"]}')
print(f'  stop_loss_daily={best_params["stop_loss_daily"]}')
print(f'  预测收益: {best_pred:+.2f}%')

print(f'\nTop 10 预测:')
for i, (pred, h, sl, tp, sld) in enumerate(top_results[:10]):
    print(f'  #{i+1} pred={pred:+.2f}%  h={h:>2}d sl={sl:>6.3f} tp={tp:.3f} sld={sld:>6.3f}')

# ====== 特征重要性 ======
print(f'\n特征重要性 (RF):')
names = ['hold_days', 'stop_loss_total', 'take_profit', 'stop_loss_daily']
for name, imp in sorted(zip(names, rf.feature_importances_), key=lambda x: -x[1]):
    print(f'  {name}: {imp:.4f}')

# ====== 验证最优解 ======
print(f'\n=== 回测验证最优解 ===')
if best_params:
    result = run_one(best_params)
    print(f'  实际收益: {result["return"]:+.2f}%')
    print(f'  最大回撤: {result["drawdown"]:.2f}%')
    print(f'  胜率: {result["win_rate"]:.1f}%')
    print(f'  交易笔数: {result["trades"]}')
    print(f'  盈亏比: {result["profit_factor"]:.2f}')

print('\n[DONE]')
