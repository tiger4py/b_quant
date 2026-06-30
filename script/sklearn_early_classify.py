# -*- coding: utf-8 -*-
"""
V2 持仓期逐日分类器 — 能否提早识别失败交易？
==============================================
对197笔已平仓交易，逐日提取持仓期特征：
  - 当前浮盈/浮亏 %
  - 最大浮盈历史
  - 最大浮亏历史
  - 当日涨跌
  - 持仓天数
  - 相对买入日量比
  - 是否连续N日阴线
  - 距买入价偏离

目标：在第N天预测最终是盈是亏
找最早可分辨的时间点

用法:
  python script/sklearn_early_classify.py
"""

import json
import sys
import re
from pathlib import Path
from datetime import datetime
from statistics import mean, median
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import numpy as np
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score, StratifiedKFold
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
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
print(f"已平仓交易: {len(closed)} 笔")

# ============ 2. 逐日提取持仓期特征 ============

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
sess = Session()

# 收集每一天的快照
# 每条记录: (trade_idx, hold_day, features..., is_win)
snapshots = []

for idx, t in enumerate(closed):
    code = t['code']
    buy_date_str = t['buy_date']
    sell_date_str = t['sell_date']
    buy_price = t['buy_price']
    sell_price = t['sell_price']
    is_win = 1 if t['profit'] > 0 else 0

    # 查持仓期间的所有日K线
    bars = sess.query(StockDaily).filter(
        StockDaily.code == code,
        StockDaily.trade_date > buy_date_str,
        StockDaily.trade_date <= sell_date_str
    ).order_by(StockDaily.trade_date).all()

    if len(bars) == 0:
        continue

    max_profit_pct = -999.0
    max_loss_pct = 999.0
    peak_close = buy_price     # 史上最高收盘价
    consecutive_red = 0        # 连续阳线
    consecutive_green = 0      # 连续阴线
    prev_close = buy_price

    for day_i, bar in enumerate(bars):
        hold_day = day_i + 1
        close = bar.close
        open_p = bar.open
        high_p = bar.high
        low_p = bar.low
        volume = bar.volume or 0
        amount = bar.amount or 0

        # 当前浮盈浮亏
        float_pnl = (close / buy_price - 1) * 100
        daily_chg = (close / prev_close - 1) * 100 if prev_close > 0 else 0

        # 史上最大浮盈/浮亏 + 最高收盘价
        if float_pnl > max_profit_pct:
            max_profit_pct = float_pnl
        if float_pnl < max_loss_pct:
            max_loss_pct = float_pnl
        if close > peak_close:
            peak_close = close

        # 连续阴阳线
        is_red = close > open_p
        if is_red:
            consecutive_red += 1
            consecutive_green = 0
        else:
            consecutive_green += 1
            consecutive_red = 0

        # 影线
        body = abs(close - open_p)
        upper_shadow = high_p - max(open_p, close)
        lower_shadow = min(open_p, close) - low_p
        upper_shadow_ratio = upper_shadow / body if body > 0.001 else 0.0
        lower_shadow_ratio = lower_shadow / body if body > 0.001 else 0.0

        # 距高点回撤
        drawdown_from_peak = (close / peak_close - 1) * 100 if peak_close > 0 else 0.0

        features = {
            # 核心
            'hold_day': hold_day,
            'float_pnl': float_pnl,                    # 当前浮盈%
            'max_profit': max_profit_pct,              # 史上最大浮盈
            'max_loss': max_loss_pct,                  # 史上最大浮亏
            'drawdown_from_peak': drawdown_from_peak,  # 距最高点回撤

            # 当日
            'daily_chg': daily_chg,                    # 当日涨跌%
            'is_red': 1 if is_red else 0,             # 收阳
            'body_pct': body / open_p * 100,           # 实体%

            # 影线
            'upper_shadow_ratio': upper_shadow_ratio,
            'lower_shadow_ratio': lower_shadow_ratio,

            # 连续
            'consecutive_red': consecutive_red,        # 连阳天数
            'consecutive_green': consecutive_green,    # 连阴天数

            # 相对买入日
            'close_vs_entry': close / buy_price,       # 相对买入价
            'high_vs_entry': high_p / buy_price,       # 最高相对买入价

            # 元数据
            'trade_idx': idx,
            'is_win': is_win,
            'hold_day_total': len(bars),               # 这笔的总持天
        }

        snapshots.append(features)
        prev_close = close

sess.close()
print(f"持仓期快照总数: {len(snapshots)}")

# ============ 3. 按持仓天数分组训练 ============

# 只看 hold_day <= 15 的快照（大多数交易在15天内）
feature_cols = [
    'float_pnl', 'max_profit', 'max_loss', 'drawdown_from_peak',
    'daily_chg', 'is_red', 'body_pct',
    'consecutive_red', 'consecutive_green',
    'close_vs_entry', 'high_vs_entry',
]

print(f"\n{'='*80}")
print(f"逐日分类 — 持仓第N天能否预测最终盈亏？")
print(f"{'='*80}")
print(f"{'Day':<6s} {'样本':<6s} {'胜率':<6s} {'Acc':<8s} {'Prec':<8s} {'Rec':<8s} {'F1':<8s} {'CV±':<8s} {'Top特征'}")
print("-" * 100)

results_by_day = []

for hold_day in range(1, 31):  # 看前30天
    day_snaps = [s for s in snapshots if s['hold_day'] == hold_day]

    if len(day_snaps) < 20:
        continue

    X = np.array([[s[c] for c in feature_cols] for s in day_snaps])
    y = np.array([s['is_win'] for s in day_snaps])

    win_rate = y.mean()

    # 用 LogisticRegression 更稳定（小样本）
    clf = LogisticRegression(max_iter=1000, C=0.5, random_state=42)

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_acc = cross_val_score(clf, X, y, cv=skf, scoring='accuracy')
    cv_prec = cross_val_score(clf, X, y, cv=skf, scoring='precision')
    cv_rec = cross_val_score(clf, X, y, cv=skf, scoring='recall')
    cv_f1 = cross_val_score(clf, X, y, cv=skf, scoring='f1')

    # 也试 RF
    rf = RandomForestClassifier(n_estimators=200, max_depth=4, random_state=42, n_jobs=-1)
    rf.fit(X, y)
    top_feat = feature_cols[np.argmax(rf.feature_importances_)]

    results_by_day.append({
        'day': hold_day,
        'n': len(day_snaps),
        'win_rate': win_rate,
        'acc': cv_acc.mean(),
        'acc_std': cv_acc.std(),
        'prec': cv_prec.mean(),
        'rec': cv_rec.mean(),
        'f1': cv_f1.mean(),
        'top_feat': top_feat,
    })

    print(f"  D{hold_day:<4d} {len(day_snaps):<6d} {win_rate:.0%}    "
          f"{cv_acc.mean():.3f}    {cv_prec.mean():.3f}    {cv_rec.mean():.3f}    {cv_f1.mean():.3f}    "
          f"±{cv_acc.std():.3f}   {top_feat}")

# ============ 4. 关键发现 ============

print(f"\n{'='*80}")
print("关键发现")
print(f"{'='*80}")

# 找准确率最高的天
best = max(results_by_day, key=lambda x: x['acc'])
print(f"\n最高准确率: Day {best['day']} (Acc={best['acc']:.1%}, F1={best['f1']:.1%})")

# 找 F1 超过 0.55 的第一天
for r in results_by_day:
    if r['f1'] > 0.55:
        print(f"首次 F1>0.55: Day {r['day']} (n={r['n']}, Acc={r['acc']:.1%})")
        break

# 按浮盈分段分析
print(f"\n--- 按浮盈分段的胜率 ---")
pnl_snaps = [s for s in snapshots if s['hold_day'] >= 3]
for pnl_range, lo, hi in [('浮亏>5%', -99, -5), ('浮亏3-5%', -5, -3),
                            ('浮亏1-3%', -3, -1), ('浮亏0-1%', -1, 0),
                            ('浮盈0-2%', 0, 2), ('浮盈2-5%', 2, 5),
                            ('浮盈>5%', 5, 999)]:
    subset = [s for s in pnl_snaps if lo <= s['float_pnl'] < hi]
    if len(subset) >= 15:
        wr = sum(1 for s in subset if s['is_win']) / len(subset)
        print(f"  {pnl_range:<12s}: {len(subset):>4d}笔  最终胜率={wr:.1%}")

# ============ 5. 多日特征：看趋势 ============

print(f"\n--- 多日趋势特征 ---")
# 对每笔交易，取第3、5、8、12天的快照
trade_features = []
for idx in range(len(closed)):
    t_snaps = [s for s in snapshots if s['trade_idx'] == idx]
    if len(t_snaps) < 3:
        continue

    feat = {'is_win': t_snaps[0]['is_win'], 'total_hold': len(t_snaps)}

    # 第3天特征
    d3 = t_snaps[2] if len(t_snaps) > 2 else None
    if d3:
        feat['d3_pnl'] = d3['float_pnl']
        feat['d3_max_profit'] = d3['max_profit']
        feat['d3_chg'] = d3['daily_chg']

    # 前3天趋势
    if len(t_snaps) >= 3:
        pnls_3d = [s['float_pnl'] for s in t_snaps[:3]]
        feat['pnl_trend_3d'] = pnls_3d[-1] - pnls_3d[0]  # 正=改善
        feat['red_days_3d'] = sum(1 for s in t_snaps[:3] if s['is_red'])

    # 第5天
    d5 = t_snaps[4] if len(t_snaps) > 4 else None
    if d5:
        feat['d5_pnl'] = d5['float_pnl']
        feat['d5_max_profit'] = d5['max_profit']

    if len(t_snaps) >= 5:
        pnls_5d = [s['float_pnl'] for s in t_snaps[:5]]
        feat['pnl_trend_5d'] = pnls_5d[-1] - pnls_5d[0]

    trade_features.append(feat)

# 训练一个"第3天判断"模型
tf_cols = ['d3_pnl', 'd3_max_profit', 'd3_chg', 'pnl_trend_3d', 'red_days_3d']
valid_tf = [t for t in trade_features if all(t.get(c) is not None for c in tf_cols)]

if len(valid_tf) >= 30:
    X_tf = np.array([[t[c] for c in tf_cols] for t in valid_tf])
    y_tf = np.array([t['is_win'] for t in valid_tf])

    clf_tf = LogisticRegression(max_iter=1000, random_state=42)
    cv_acc = cross_val_score(clf_tf, X_tf, y_tf, cv=5, scoring='accuracy')
    cv_f1 = cross_val_score(clf_tf, X_tf, y_tf, cv=5, scoring='f1')
    print(f"第3天模型: {len(valid_tf)}笔, Acc={cv_acc.mean():.2%}±{cv_acc.std():.2%}, F1={cv_f1.mean():.2%}")

    # 重要特征
    lr = LogisticRegression(max_iter=1000, random_state=42)
    lr.fit(X_tf, y_tf)
    coefs = lr.coef_[0]
    print("  特征权重:")
    for name, coef in sorted(zip(tf_cols, coefs), key=lambda x: -abs(x[1])):
        print(f"    {name:<20s}: {coef:+.4f}")

# 第5天模型
tf_cols_5 = ['d3_pnl', 'd5_pnl', 'd3_max_profit', 'd5_max_profit', 'pnl_trend_5d', 'pnl_trend_3d']
valid_tf5 = [t for t in trade_features if all(t.get(c) is not None for c in tf_cols_5)]

if len(valid_tf5) >= 30:
    X_tf5 = np.array([[t[c] for c in tf_cols_5] for t in valid_tf5])
    y_tf5 = np.array([t['is_win'] for t in valid_tf5])

    clf_tf5 = LogisticRegression(max_iter=1000, random_state=42)
    cv_acc5 = cross_val_score(clf_tf5, X_tf5, y_tf5, cv=5, scoring='accuracy')
    cv_f15 = cross_val_score(clf_tf5, X_tf5, y_tf5, cv=5, scoring='f1')
    print(f"\n第5天模型: {len(valid_tf5)}笔, Acc={cv_acc5.mean():.2%}±{cv_acc5.std():.2%}, F1={cv_f15.mean():.2%}")

    lr5 = LogisticRegression(max_iter=1000, random_state=42)
    lr5.fit(X_tf5, y_tf5)
    coefs5 = lr5.coef_[0]
    print("  特征权重:")
    for name, coef in sorted(zip(tf_cols_5, coefs5), key=lambda x: -abs(x[1])):
        print(f"    {name:<20s}: {coef:+.4f}")

# ============ 6. 硬规则探索 ============

print(f"\n{'='*80}")
print("硬规则探索 — 简单条件能否筛选出失败交易？")
print(f"{'='*80}")

rules_to_test = [
    ("Day3浮亏>5%", lambda s: s['hold_day'] == 3 and s['float_pnl'] < -5),
    ("Day3浮亏>3%", lambda s: s['hold_day'] == 3 and s['float_pnl'] < -3),
    ("Day3浮亏>2%", lambda s: s['hold_day'] == 3 and s['float_pnl'] < -2),
    ("Day5浮亏>5%", lambda s: s['hold_day'] == 5 and s['float_pnl'] < -5),
    ("Day5浮亏>3%", lambda s: s['hold_day'] == 5 and s['float_pnl'] < -3),
    ("Day3-5连阴", lambda s: s['hold_day'] == 5 and s['consecutive_green'] >= 3),
    ("Day3未浮盈过", lambda s: s['hold_day'] == 3 and s['max_profit'] < 0),
    ("Day5未浮盈过", lambda s: s['hold_day'] == 5 and s['max_profit'] < 0),
    ("Day3距高回撤>5%", lambda s: s['hold_day'] == 3 and s['drawdown_from_peak'] < -5),
    ("Day5距高回撤>8%", lambda s: s['hold_day'] == 5 and s['drawdown_from_peak'] < -8),
    ("Day3浮盈+阳线", lambda s: s['hold_day'] == 3 and s['float_pnl'] > 0 and s['is_red'] == 1),
    ("Day3浮盈>2%", lambda s: s['hold_day'] == 3 and s['float_pnl'] > 2),
]

for rule_name, rule_fn in rules_to_test:
    matched = [s for s in snapshots if rule_fn(s)]
    if len(matched) < 5:
        continue
    matched_wins = sum(1 for s in matched if s['is_win'])
    matched_losses = len(matched) - matched_wins
    final_wr = matched_wins / len(matched)

    # 这个规则能筛出多少失败交易？
    all_losses = sum(1 for s in snapshots if s['is_win'] == 0
                     and s['hold_day'] == matched[0]['hold_day'])
    recall_of_losses = matched_losses / all_losses if all_losses > 0 else 0

    print(f"  {rule_name:<20s}: {len(matched):>3d}笔  "
          f"最终胜率{final_wr:.0%}  "
          f"捕获{matched_losses}笔失败 "
          f"(召回{recall_of_losses:.0%})")

print("\n完成!")
