# -*- coding: utf-8 -*-
"""
V2 大跌抄底 — sklearn 特征重要性 + 梯度方向搜索
=================================================
对198笔已平仓交易做：
  1. 特征提取（buy_reason + DB补充）
  2. RandomForest 特征重要性排序
  3. 偏依赖图 — 找每个参数的最优方向
  4. 卖出参数网格搜索

用法:
  python script/sklearn_analyze_v2.py
"""

import json
import sys
import re
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from statistics import mean, median

sys.stdout.reconfigure(encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import numpy as np
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.model_selection import cross_val_score
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models.stock import StockDaily
from config import DATABASE_URL

# ============ 1. 加载交易数据 ============

JSON_PATH = ROOT_DIR / 'data/strategy/dip_hunting/2026-06/2026-06-26_03.json'

with open(JSON_PATH, 'r', encoding='utf-8') as f:
    data = json.load(f)

trades = data['trades']
closed = [t for t in trades if '期末持仓' not in t.get('sell_reason', '')]
print(f"加载交易: {len(trades)} 笔, 已平仓: {len(closed)} 笔")


# ============ 2. 特征提取 ============

def parse_buy_reason(reason):
    """从买入原因提取数值特征"""
    feats = {}
    if not reason:
        return feats

    # 5日跌幅
    m = re.search(r'5日跌(-?[\d.]+)%', reason)
    if m: feats['chg_5d'] = float(m.group(1))

    # 距20日高点跌幅
    m = re.search(r'距高(-?[\d.]+)%', reason)
    if m: feats['decline_from_high'] = float(m.group(1))

    # 量比
    m = re.search(r'量([\d.]+)x', reason)
    if m: feats['vol_ratio'] = float(m.group(1))

    # 缩量比
    m = re.search(r'缩([\d.]+)', reason)
    if m: feats['vol_decline_ratio'] = float(m.group(1))

    # RSI
    m = re.search(r'RSI([\d.]+)', reason)
    if m: feats['rsi'] = float(m.group(1))

    # 下影线比
    m = re.search(r'影([\d.]+)x', reason)
    if m: feats['shadow_ratio'] = float(m.group(1))

    # 确认日涨幅
    m = re.search(r'确认涨([\d.]+)%', reason)
    if m: feats['confirm_chg'] = float(m.group(1))

    # 低点
    m = re.search(r'低点([\d.]+)', reason)
    if m: feats['dip_low'] = float(m.group(1))

    return feats


def parse_sell_reason(reason):
    """从卖出原因提取信息"""
    feats = {}
    if not reason:
        return feats

    # 盈亏
    m = re.search(r'盈(-?[\d.]+)%', reason)
    if m: feats['exit_profit'] = float(m.group(1))
    m = re.search(r'\((-?[\d.]+)%', reason)
    if m: feats['exit_profit_val'] = float(m.group(1))

    # 高点
    m = re.search(r'高([\d.]+)', reason)
    if m: feats['peak_price'] = float(m.group(1))
    m = re.search(r'回(-?[\d.]+)%', reason)
    if m: feats['drawdown_from_peak'] = float(m.group(1))

    # 持有天数
    m = re.search(r'持(\d+)天', reason)
    if m: feats['hold_days'] = int(m.group(1))
    m = re.search(r'持仓(\d+)天', reason)
    if m: feats['hold_days'] = int(m.group(1))

    # 止损
    m = re.search(r'止损(-?[\d.]+)%', reason)
    if m: feats['stop_pct'] = float(m.group(1))

    # 低点
    m = re.search(r'低点缓冲([\d.]+)', reason)
    if m: feats['dip_low_buffered'] = float(m.group(1))

    return feats


# 提取特征矩阵
rows = []
for t in closed:
    buy = parse_buy_reason(t.get('buy_reason', ''))
    sell = parse_sell_reason(t.get('sell_reason', ''))

    # 持有天数
    try:
        bd = datetime.strptime(t['buy_date'], '%Y-%m-%d')
        sd = datetime.strptime(t['sell_date'], '%Y-%m-%d')
        hold_days = (sd - bd).days
    except:
        hold_days = 0

    row = {
        'code': t['code'],
        'name': t['name'],
        'buy_date': t['buy_date'],
        'sell_date': t['sell_date'],
        'profit_pct': t['profit_pct'],
        'profit': t['profit'],
        'is_win': 1 if t['profit'] > 0 else 0,

        # 买入特征
        'chg_5d': buy.get('chg_5d', None),
        'decline_from_high': buy.get('decline_from_high', None),
        'vol_ratio': buy.get('vol_ratio', None),
        'vol_decline_ratio': buy.get('vol_decline_ratio', None),
        'rsi': buy.get('rsi', None),
        'shadow_ratio': buy.get('shadow_ratio', None),
        'confirm_chg': buy.get('confirm_chg', None),
        'dip_low': buy.get('dip_low', None),

        # 卖出特征
        'hold_days': hold_days,
        'sell_type': '止损' if '止损' in t.get('sell_reason', '') and '时间' not in t.get('sell_reason', '')
                     else '时间止损' if '时间止损' in t.get('sell_reason', '')
                     else '止盈' if '止盈' in t.get('sell_reason', '')
                     else '抄底失败' if '抄底失败' in t.get('sell_reason', '')
                     else '到期' if '到期' in t.get('sell_reason', '')
                     else '其他',
    }
    rows.append(row)

print(f"有效特征行: {len(rows)}")

# 检查特征完整度
for feat in ['chg_5d', 'decline_from_high', 'vol_ratio', 'vol_decline_ratio',
             'rsi', 'shadow_ratio', 'confirm_chg', 'dip_low']:
    present = sum(1 for r in rows if r[feat] is not None)
    print(f"  {feat}: {present}/{len(rows)} 有效")


# ============ 3. 补充DB数据：买入时市场环境 ============

print("\n查询数据库补充市场环境...")
engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

# 获取每笔交易买入日的市场数据（从最近的 market_stats）
# 简化：查买入日大盘指数涨跌
for r in rows:
    buy_date = r['buy_date']
    # 查上证指数当日涨跌
    sh_row = sess.query(StockDaily).filter(
        StockDaily.code == 'sh.000001',
        StockDaily.trade_date == buy_date
    ).first()
    if sh_row:
        r['sh_close'] = sh_row.close
        # 前一日
        prev = sess.query(StockDaily).filter(
            StockDaily.code == 'sh.000001',
            StockDaily.trade_date < buy_date
        ).order_by(StockDaily.trade_date.desc()).first()
        if prev and prev.close > 0:
            r['sh_chg'] = (sh_row.close / prev.close - 1) * 100
        else:
            r['sh_chg'] = 0
    else:
        r['sh_close'] = None
        r['sh_chg'] = 0

    # 查个股买入日前20日波动率
    stock_bars = sess.query(StockDaily).filter(
        StockDaily.code == r['code'],
        StockDaily.trade_date <= buy_date
    ).order_by(StockDaily.trade_date.desc()).limit(20).all()

    if len(stock_bars) >= 10:
        chgs = []
        for i in range(1, len(stock_bars)):
            if stock_bars[i].close > 0:
                chgs.append((stock_bars[i-1].close / stock_bars[i].close - 1) * 100)
        r['volatility_20d'] = np.std(chgs) if chgs else None

        # 距MA60
        closes = [b.close for b in reversed(stock_bars)]
        ma60_bars = sess.query(StockDaily).filter(
            StockDaily.code == r['code'],
            StockDaily.trade_date <= buy_date
        ).order_by(StockDaily.trade_date.desc()).limit(60).all()
        if len(ma60_bars) >= 60:
            ma60 = sum(b.close for b in ma60_bars) / 60
            r['below_ma60'] = (stock_bars[0].close / ma60 - 1) * 100
        else:
            r['below_ma60'] = None

        # 个股当日涨幅
        if len(stock_bars) >= 2 and stock_bars[1].close > 0:
            r['stock_daily_chg'] = (stock_bars[0].close / stock_bars[1].close - 1) * 100
        else:
            r['stock_daily_chg'] = None
    else:
        r['volatility_20d'] = None
        r['below_ma60'] = None
        r['stock_daily_chg'] = None

sess.close()
print("数据库查询完成")

# 检查新增特征完整度
for feat in ['sh_chg', 'volatility_20d', 'below_ma60', 'stock_daily_chg']:
    present = sum(1 for r in rows if r[feat] is not None)
    print(f"  {feat}: {present}/{len(rows)} 有效")


# ============ 4. RandomForest 特征重要性 ============

print("\n" + "=" * 70)
print("RandomForest 特征重要性分析")
print("=" * 70)

# 选择特征列
feature_cols = [
    'chg_5d',           # 5日跌幅
    'decline_from_high', # 距20日高跌幅
    'vol_ratio',         # 恐慌放量比
    'vol_decline_ratio', # 量能萎缩比
    'rsi',              # RSI超卖
    'shadow_ratio',      # 下影线比
    'confirm_chg',       # 确认日涨幅
    'sh_chg',            # 买入日上证涨跌
    'volatility_20d',    # 个股20日波动率
    'below_ma60',        # 距MA60%
    'stock_daily_chg',   # 个股当日涨幅
    'hold_days',         # 持有天数
]

# 构建 X, y
valid_rows = [r for r in rows if all(r.get(c) is not None for c in feature_cols)]
print(f"完整特征样本: {len(valid_rows)} / {len(rows)}")

if len(valid_rows) < 50:
    # 放宽要求，只用买入特征
    feature_cols = ['chg_5d', 'decline_from_high', 'vol_ratio', 'vol_decline_ratio',
                    'rsi', 'shadow_ratio', 'confirm_chg']
    valid_rows = [r for r in rows if all(r.get(c) is not None for c in feature_cols)]
    print(f"使用买入特征: {len(valid_rows)} 样本")

X = np.array([[r[c] for c in feature_cols] for r in valid_rows])
y = np.array([r['is_win'] for r in valid_rows])
y_reg = np.array([r['profit_pct'] for r in valid_rows])

print(f"\n特征矩阵: {X.shape}")
print(f"胜率: {y.mean()*100:.1f}%")

# ---- 4a. 分类器特征重要性 ----
clf = RandomForestClassifier(n_estimators=500, max_depth=5, random_state=42, n_jobs=-1)
clf.fit(X, y)

print(f"\n分类器 OOB score: {clf.score(X, y):.3f} (train)")
cv_scores = cross_val_score(clf, X, y, cv=5)
print(f"5-fold CV: {cv_scores.mean():.3f} +/- {cv_scores.std():.3f}")

# 特征重要性（Gini + Permutation）
gini_imp = clf.feature_importances_
perm_result = permutation_importance(clf, X, y, n_repeats=30, random_state=42)
perm_imp = perm_result.importances_mean

print(f"\n{'特征':<22s} {'Gini重要度':>10s} {'Perm重要度':>10s}")
print("-" * 50)
ranked = sorted(zip(feature_cols, gini_imp, perm_imp), key=lambda x: -x[1])
for name, gini, perm in ranked:
    print(f"  {name:<20s} {gini:10.4f} {perm:10.4f}")


# ---- 4b. 回归器（预测盈亏幅度）----
reg = RandomForestRegressor(n_estimators=500, max_depth=5, random_state=42, n_jobs=-1)
reg.fit(X, y_reg)

print(f"\n回归器 R2 (train): {reg.score(X, y_reg):.3f}")
print("\n回归特征重要性:")
reg_imp = reg.feature_importances_
reg_ranked = sorted(zip(feature_cols, reg_imp), key=lambda x: -x[1])
for name, imp in reg_ranked:
    print(f"  {name:<20s} {imp:10.4f}")


# ============ 5. 偏依赖分析 — 每个特征的梯度方向 ============

print("\n" + "=" * 70)
print("偏依赖分析 — 参数梯度方向")
print("=" * 70)

for col_idx, col_name in enumerate(feature_cols):
    vals = X[:, col_idx]
    p10, p25, p50, p75, p90 = np.percentile(vals, [10, 25, 50, 75, 90])

    # 在分位点评估预测胜率
    X_test = X.copy()
    scores = []
    for pct_val in [p10, p25, p50, p75, p90]:
        X_test[:, col_idx] = pct_val
        pred_win = clf.predict_proba(X_test)[:, 1].mean()
        scores.append(pred_win)

    print(f"\n  [{col_name}] 范围 [{p10:.2f} ~ {p90:.2f}]")
    print(f"    p10={p10:.2f} → 胜率={scores[0]:.1%}")
    print(f"    p25={p25:.2f} → 胜率={scores[1]:.1%}")
    print(f"    p50={p50:.2f} → 胜率={scores[2]:.1%}")
    print(f"    p75={p75:.2f} → 胜率={scores[3]:.1%}")
    print(f"    p90={p90:.2f} → 胜率={scores[4]:.1%}")

    # 梯度方向
    if scores[-1] > scores[0] + 0.02:
        direction = "↑ 增大该参数有利于提高胜率"
    elif scores[0] > scores[-1] + 0.02:
        direction = "↓ 减小该参数有利于提高胜率"
    else:
        direction = "→ 该参数不敏感"

    print(f"    方向: {direction}")


# ============ 6. 卖出参数分析 ============

print("\n" + "=" * 70)
print("卖出参数分析")
print("=" * 70)

# 6a. 止盈线 vs 利润
take_profits = [r for r in rows if r['sell_type'] == '止盈']
if take_profits:
    pcts = [r['profit_pct'] for r in take_profits]
    print(f"\n止盈组 ({len(take_profits)}笔):")
    print(f"  平均盈利: {mean(pcts):.2f}%")
    print(f"  最小: {min(pcts):.2f}%  最大: {max(pcts):.2f}%")
    print(f"  中位: {median(pcts):.2f}%")

    # 如果提高止盈线到15%
    above_15 = sum(1 for p in pcts if p > 15)
    print(f"  盈利>15%的有 {above_15} 笔 — 如果止盈设15%它们还能跑更远")

# 6b. 止损线 vs 后续走势
stop_losses = [r for r in rows if r['sell_type'] == '止损']
if stop_losses:
    pcts = [r['profit_pct'] for r in stop_losses]
    print(f"\n止损组 ({len(stop_losses)}笔):")
    print(f"  平均亏损: {mean(pcts):.2f}%")
    print(f"  最小: {min(pcts):.2f}%  最大: {max(pcts):.2f}%")

    # 如果止损放宽到-10%
    within_10 = sum(1 for p in pcts if p > -10)
    print(f"  亏损在-10%以内的有 {within_10} 笔 — 如果放宽止损到-10%可以少砍这些")

    # 止损后走势（从DB查，这里用已有分析）
    print(f"  （止损后走势见 analyze_trades Excel「卖早分析」sheet）")

# 6c. 到期组
expires = [r for r in rows if r['sell_type'] == '到期']
if expires:
    pcts = [r['profit_pct'] for r in expires]
    print(f"\n到期组 ({len(expires)}笔):")
    print(f"  平均盈亏: {mean(pcts):.2f}%")
    print(f"  胜率: {sum(1 for p in pcts if p > 0)/len(pcts)*100:.0f}%")
    print(f"  平均持有: {mean([r['hold_days'] for r in expires]):.0f} 天")

# 6d. 抄底失败组
dip_fails = [r for r in rows if r['sell_type'] == '抄底失败']
if dip_fails:
    print(f"\n抄底失败组 ({len(dip_fails)}笔):")
    print(f"  平均亏损: {mean([r['profit_pct'] for r in dip_fails]):.2f}%")
    print(f"  平均持有: {mean([r['hold_days'] for r in dip_fails]):.0f} 天")


# ============ 7. 组合逻辑分析 ============

print("\n" + "=" * 70)
print("多条件组合分析 — 哪些条件真正在起作用")
print("=" * 70)

# 分析：满足更严格条件的子集是否胜率更高？
conditions = {
    'chg_5d < -12%':      lambda r: r['chg_5d'] is not None and r['chg_5d'] < -12,
    'chg_5d < -15%':      lambda r: r['chg_5d'] is not None and r['chg_5d'] < -15,
    'decline > 20%':      lambda r: r['decline_from_high'] is not None and r['decline_from_high'] < -20,
    'RSI < 30':           lambda r: r['rsi'] is not None and r['rsi'] < 30,
    'RSI < 25':           lambda r: r['rsi'] is not None and r['rsi'] < 25,
    'vol_ratio > 1.5':    lambda r: r['vol_ratio'] is not None and r['vol_ratio'] > 1.5,
    'vol_ratio > 2.0':    lambda r: r['vol_ratio'] is not None and r['vol_ratio'] > 2.0,
    'shadow > 2.0':       lambda r: r['shadow_ratio'] is not None and r['shadow_ratio'] > 2.0,
    'confirm > 1.0%':     lambda r: r['confirm_chg'] is not None and r['confirm_chg'] > 1.0,
    'confirm > 1.5%':     lambda r: r['confirm_chg'] is not None and r['confirm_chg'] > 1.5,
}

for cond_name, cond_fn in conditions.items():
    subset = [r for r in rows if cond_fn(r)]
    if len(subset) >= 10:
        win_rate = sum(1 for r in subset if r['is_win']) / len(subset)
        avg_profit = mean([r['profit_pct'] for r in subset])
        base_wr = sum(1 for r in rows if r['is_win']) / len(rows)
        delta = win_rate - base_wr
        print(f"  {cond_name:<22s}: {len(subset):>3d}笔  胜率{win_rate:.1%}({delta:+.1%})  均利{avg_profit:+.2f}%")
    else:
        print(f"  {cond_name:<22s}: {len(subset):>3d}笔  (样本太少)")

# ============ 8. 综合优化建议 ============

print("\n" + "=" * 70)
print("综合优化建议")
print("=" * 70)

# 找出最重要的特征
top_features = sorted(zip(feature_cols, gini_imp, perm_imp), key=lambda x: -x[1])[:5]
print("\nTop 5 最重要特征:")
for i, (name, gini, perm) in enumerate(top_features, 1):
    print(f"  {i}. {name} (Gini={gini:.4f}, Perm={perm:.4f})")

# 基于偏依赖给出建议
print("\n买入参数优化方向:")
print("  - 如果TOP1特征偏依赖显示增大有利 → 收紧该阈值")
print("  - 如果TOP1特征偏依赖显示减小有利 → 放宽该阈值")
print("  - 不敏感的特征可以去掉，简化策略")
print("\n卖出参数:")
print("  - 止损后若卖早率高 → 放宽止损（-8%→-10%或ATR动态）")
print("  - 止盈若最高可达+20% → 提高止盈（12%→15%）")
print("  - 到期组若胜率>50% → 延长持有期（15d→18d）")

print("\n完成!")
