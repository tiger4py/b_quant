"""
晓鸣股份 (sz.300967) 贝叶斯预测 - 涨跌幅度版
================================================
改进:
  1. 61维特征工程 (动量/波动率/量价/K线/均线/RSI/布林带/连涨连跌等)
  2. 互信息(MI)特征筛选 → 每个预测目标保留Top15特征
  3. 三窗口贝叶斯集成 (GaussianNB + BernoulliNB, 窗口250/400/600天)
  4. Isotonic概率校准
  5. Beta共轭先验可信度
  6. 【新增】预测涨跌幅度 (分桶: 大涨/小涨/横盘/小跌/大跌)
  7. 【新增】准确率统计: 实际涨跌<1%的样本不计入正确率

评估口径:
  - 次日涨跌: 预测方向 vs 实际方向, 实际|涨跌幅|<1%的剔除不计
  - 3日涨跌: 同理, 实际|涨跌幅|<1%的剔除不计

运行: python backtest/strategy/stock/300967_glm5.2.py
"""

import sqlite3, warnings
import numpy as np
import pandas as pd
from sklearn.naive_bayes import GaussianNB, BernoulliNB
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import mutual_info_classif
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import accuracy_score, roc_auc_score, brier_score_loss
from scipy.stats import beta as beta_dist
warnings.filterwarnings('ignore')

# ============================================================
# 1. 加载数据
# ============================================================
conn = sqlite3.connect('data/stock.db')
df = pd.read_sql(
    "SELECT trade_date, open, high, low, close, volume, amount, turn, pe_ttm "
    "FROM stock_daily WHERE code='sz.300967' ORDER BY trade_date", conn)
conn.close()
df['trade_date'] = pd.to_datetime(df['trade_date'])
df = df.reset_index(drop=True)
print(f"数据: {df['trade_date'].iloc[0].date()} ~ {df['trade_date'].iloc[-1].date()}, {len(df)} 个交易日")
print(f"最新收盘: {df['close'].iloc[-1]:.2f}")
print("=" * 75)

# ============================================================
# 2. 特征工程 (61维)
# ============================================================
c = df['close'].values.astype(float)
h = df['high'].values.astype(float)
l = df['low'].values.astype(float)
v = df['volume'].values.astype(float)
a = df['amount'].values.astype(float)
t = df['turn'].values.astype(float)
o = df['open'].values.astype(float)

def spc(arr, p):
    r = np.full(len(arr), np.nan)
    for i in range(p, len(arr)):
        if arr[i-p] != 0:
            r[i] = (arr[i]-arr[i-p]) / abs(arr[i-p])
    return r

def rsma(arr, w): return pd.Series(arr).rolling(w).mean().values
def rstd(arr, w): return pd.Series(arr).rolling(w).std().values
def rmax(arr, w): return pd.Series(arr).rolling(w).max().values
def rmin(arr, w): return pd.Series(arr).rolling(w).min().values

fe = pd.DataFrame()
fe['trade_date'] = df['trade_date']
daily_ret = spc(c, 1)

# --- A. 动量 (7个) ---
for p in [1,2,3,5,10,20]: fe[f'ret_{p}'] = spc(c, p)
fe['mom_accel'] = fe['ret_5'] - fe['ret_20']

# --- B. 波动率 (8个) ---
for w in [5,10,20]: fe[f'vstd_{w}'] = rstd(daily_ret, w)
tr = np.maximum(h-l, np.maximum(abs(h-np.roll(c,1)), abs(l-np.roll(c,1))))
fe['atr_14'] = rsma(tr,14) / np.where(c!=0,c,1e-8)
fe['atr_5']  = rsma(tr,5)  / np.where(c!=0,c,1e-8)
fe['vol_expansion'] = fe['vstd_5'] / np.where(fe['vstd_20']>0.001, fe['vstd_20'], 0.01)
up = np.where(daily_ret>0, daily_ret, 0); dn = np.where(daily_ret<0, abs(daily_ret), 0)
fe['up_vol_10'] = rsma(up,10); fe['dn_vol_10'] = rsma(dn,10)
fe['vol_asym'] = fe['up_vol_10'] / np.where(fe['dn_vol_10']>0.001, fe['dn_vol_10'], 0.01)

# --- C. 量价 (12个) ---
for p in [1,3,5,10]:
    fe[f'vchg_{p}'] = spc(v, p); fe[f'achg_{p}'] = spc(a, p)
fe['vol_ratio'] = v / np.where(rsma(v,20)>0, rsma(v,20), np.nan)
fe['vol_surge']  = (fe['vol_ratio']>1.5).astype(float)
fe['vol_shrink'] = (fe['vol_ratio']<0.5).astype(float)
fe['turn_chg_1d'] = spc(t,1)
fe['turn5'] = rsma(t,5); fe['turn10'] = rsma(t,10)
fe['turn_accel'] = (fe['turn5']-fe['turn10']) / np.where(fe['turn10']>0.001, fe['turn10'], 0.01)

# --- D. K线形态 (11个) ---
fe['up_shadow'] = (h-np.maximum(o,c)) / np.where(h!=l, h-l, 1e-8)
fe['lo_shadow'] = (np.minimum(o,c)-l) / np.where(h!=l, h-l, 1e-8)
fe['body']      = abs(c-o) / np.where(h!=l, h-l, 1e-8)
fe['is_red']    = (c>o).astype(float)
fe['is_green']  = (c<o).astype(float)
fe['doji']      = (fe['body']<0.1).astype(float)
fe['hammer']    = ((fe['lo_shadow']>0.6) & (fe['up_shadow']<0.1)).astype(float)
gap = (o-np.roll(c,1)) / np.where(np.roll(c,1)!=0, abs(np.roll(c,1)), 1e-8)
fe['gap']       = np.where(np.abs(gap)<0.5, gap, np.nan)
fe['gap_up']    = (gap>0.02).astype(float)
fe['gap_down']  = (gap<-0.02).astype(float)
fe['close_str'] = (c-l) / np.where(h!=l, h-l, 1e-8)

# --- E. 均线偏离与趋势 (7个) ---
for w in [5,10,20,60]:
    ma = rsma(c,w); fe[f'bias_{w}'] = (c-ma)/np.where(ma!=0, ma, 1e-8)
for w in [5,10,20]:
    ma = rsma(c,w); fe[f'ma_slp_{w}'] = spc(ma, w)
ma5, ma10, ma20 = rsma(c,5), rsma(c,10), rsma(c,20)
fe['ma_align'] = ((ma5>ma10).astype(float) + (ma10>ma20).astype(float)) / 2.0

# --- F. 价格位置 (5个) ---
for w in [5,10,20]:
    rh, rl = rmax(c,w), rmin(c,w)
    fe[f'pos_{w}'] = (c-rl) / np.where(rh!=rl, rh-rl, 1e-8)
fe['dist_high_20'] = (c - rmax(c,20)) / np.where(rmax(c,20)!=0, abs(rmax(c,20)), 1e-8)
fe['max_dd_10'] = c / np.where(rmax(c,10)!=0, rmax(c,10), 1e-8) - 1

# --- G. RSI (2个) ---
def rsi(close, p):
    delta = np.diff(close, prepend=close[0])
    g = np.where(delta>0, delta, 0); ls = np.where(delta<0, -delta, 0)
    ag, al = rsma(g,p), rsma(ls,p)
    rs = ag / np.where(al>0, al, 1e-8)
    return 100 - 100/(1+rs)
fe['rsi_6']  = rsi(c,6)
fe['rsi_14'] = rsi(c,14)

# --- H. 布林带位置 (1个) ---
bb_std = rstd(c,20); ma20v = rsma(c,20)
fe['bb_pos'] = (c - (ma20v - 2*bb_std)) / np.where(4*bb_std>0.001, 4*bb_std, 0.01)

# --- I. 连涨连跌 (1个) ---
consec = []; cnt = 0
for i in range(len(c)):
    if i == 0: cnt = 0
    elif c[i] > c[i-1]: cnt = max(cnt+1,1) if cnt>=0 else 1
    elif c[i] < c[i-1]: cnt = min(cnt-1,-1) if cnt<=0 else -1
    else: cnt = 0
    consec.append(cnt)
fe['consec'] = np.array(consec, dtype=float)

# --- J. 波动率期限结构 (2个) ---
vstd60 = rstd(daily_ret, 60)
fe['vol_term'] = fe['vstd_5'] / np.where(fe['vstd_20']>0.001, fe['vstd_20'], np.nan)
fe['vol_regime'] = fe['vstd_5'] - vstd60

# ============================================================
# 3. 标签: 涨跌方向 + 实际涨跌幅度
# ============================================================
# 实际涨跌幅度
fe['actual_chg_1d'] = spc(c, 1)   # 未来1天实际涨跌幅
fe['actual_chg_3d'] = (np.roll(c, -3) - c) / np.where(c != 0, c, 1e-8)  # 未来3天
fe['label_1d'] = (np.roll(c,-1) > c).astype(int)
fe['label_3d'] = (np.roll(c,-3) > c).astype(int)

all_feats = [x for x in fe.columns if x not in ['trade_date','label_1d','label_3d',
                                                  'actual_chg_1d','actual_chg_3d']]
data = fe.dropna().reset_index(drop=True)
print(f"样本: {len(data)} 行, 原始特征: {len(all_feats)}")
print(f"标签1d: 涨={data['label_1d'].sum()}, 跌={len(data)-data['label_1d'].sum()} ({data['label_1d'].mean():.1%})")
print(f"标签3d: 涨={data['label_3d'].sum()}, 跌={len(data)-data['label_3d'].sum()} ({data['label_3d'].mean():.1%})")
print("=" * 75)

# ============================================================
# 4. 特征筛选 (互信息)
# ============================================================
print("\n[特征筛选 - 互信息 Mutual Information]")
print("-" * 50)
X_all = data[all_feats].values

def select_top_features(label_col, top_k=15):
    mi = mutual_info_classif(X_all, data[label_col].values, random_state=42)
    idx = np.argsort(mi)[-top_k:]
    feats = [all_feats[i] for i in idx]
    return feats, mi

sel_1d, mi_1d = select_top_features('label_1d', 15)
sel_3d, mi_3d = select_top_features('label_3d', 15)

print("\n1d预测 Top-10 特征:")
for i in np.argsort(mi_1d)[-10:][::-1]:
    print(f"  {all_feats[i]:<22} MI={mi_1d[i]:.4f}")
print("\n3d预测 Top-10 特征:")
for i in np.argsort(mi_3d)[-10:][::-1]:
    print(f"  {all_feats[i]:<22} MI={mi_3d[i]:.4f}")

# ============================================================
# 5. 三窗口贝叶斯集成 + 涨跌幅度预测
# ============================================================
def ensemble_predict_with_mag(data, feats, label_col, chg_col, horizon_name,
                             windows=(250,400,600), threshold=0.01):
    """
    三窗口贝叶斯集成, 额外输出:
      - 分桶预测: 大涨(>2%)/小涨(1~2%)/横盘/小跌(-2~-1%)/大跌(<-2%)
      - 准确率: 剔除实际|涨跌幅|<threshold的样本
      - 幅度预测误差: MAE / RMSE
    """
    X = data[feats].values
    y = data[label_col].values
    actual_chg = data[chg_col].values
    n = len(X)
    min_w = min(windows)

    raw_probs = np.full(n - min_w, np.nan)
    y_true = y[min_w:]
    actual_chg_test = actual_chg[min_w:]

    for i in range(min_w, n):
        x_test = X[i:i+1]
        wprobs = []
        for w in windows:
            if i - w < 0: continue
            Xtr_raw = X[i-w:i]; ytr = y[i-w:i]
            sc = StandardScaler()
            Xtr = sc.fit_transform(Xtr_raw); Xte = sc.transform(x_test)
            gnb = GaussianNB(var_smoothing=1e-8).fit(Xtr, ytr)
            bnb = BernoulliNB(alpha=0.5).fit(Xtr, ytr)
            pg = gnb.predict_proba(Xte)[0,1]
            pb = bnb.predict_proba(Xte)[0,1]
            wprobs.append((pg+pb)/2)
        raw_probs[i - min_w] = np.mean(wprobs)

    # Isotonic 校准
    split = int(len(raw_probs) * 0.6)
    iso = IsotonicRegression(out_of_bounds='clip', y_min=0.02, y_max=0.98)
    iso.fit(raw_probs[:split], y_true[:split])
    cal_probs = iso.predict(raw_probs)

    preds = (cal_probs > 0.5).astype(int)

    # ========== 核心: 剔除|涨跌幅|<threshold的样本 ==========
    valid_mask = np.abs(actual_chg_test) >= threshold
    n_valid = valid_mask.sum()
    n_excluded = (~valid_mask).sum()

    if n_valid > 0:
        preds_valid = preds[valid_mask]
        y_true_valid = y_true[valid_mask]
        chg_valid = actual_chg_test[valid_mask]
        cal_valid = cal_probs[valid_mask]
        acc_valid = accuracy_score(y_true_valid, preds_valid)
        try:
            auc_valid = roc_auc_score(y_true_valid, cal_valid)
        except:
            auc_valid = None
    else:
        acc_valid = None
        auc_valid = None

    # 全样本准确率 (对比用)
    acc_all = accuracy_score(y_true, preds)
    try:
        auc_all = roc_auc_score(y_true, cal_probs)
    except:
        auc_all = None

    # ========== 分桶统计 ==========
    # 预测概率 → 分桶
    bucket_map = []
    for p in cal_probs:
        if p > 0.65:
            bucket_map.append('大涨(>2%)')
        elif p > 0.55:
            bucket_map.append('小涨(1~2%)')
        elif p >= 0.45:
            bucket_map.append('横盘/观望')
        elif p >= 0.35:
            bucket_map.append('小跌(-2~-1%)')
        else:
            bucket_map.append('大跌(<-2%)')
    bucket_map = np.array(bucket_map)

    # 实际涨跌 → 分桶
    actual_bucket = []
    for ch in actual_chg_test:
        if ch > 0.02:
            actual_bucket.append('大涨(>2%)')
        elif ch > 0.01:
            actual_bucket.append('小涨(1~2%)')
        elif ch > -0.01:
            actual_bucket.append('横盘/观望')
        elif ch > -0.02:
            actual_bucket.append('小跌(-2~-1%)')
        else:
            actual_bucket.append('大跌(<-2%)')
    actual_bucket = np.array(actual_bucket)

    # 分桶准确率 (仅统计有效样本: 实际|涨跌|>=1%)
    bucket_acc = {}
    for bk in ['大涨(>2%)', '小涨(1~2%)', '小跌(-2~-1%)', '大跌(<-2%)']:
        mask = (bucket_map == bk) & valid_mask
        if mask.sum() > 0:
            # 预测"涨"类 vs 实际
            actual_dir = (actual_chg_test[mask] > 0).astype(int)
            pred_dir = preds[mask]
            correct = (pred_dir == actual_dir).sum()
            total = mask.sum()
            bucket_acc[bk] = (correct, total, correct/total)
        else:
            bucket_acc[bk] = (0, 0, None)

    # ========== 幅度预测 ==========
    # 用校准概率估算涨跌幅度: prob 0.5 → 0%, prob 1.0 → +3%, prob 0.0 → -3%
    # 线性映射 (简单但直观)
    pred_mag = (cal_probs - 0.5) * 6.0  # ±3%
    # 仅看有效样本的幅度误差
    if n_valid > 0:
        mae = np.mean(np.abs(pred_mag[valid_mask] - actual_chg_test[valid_mask] * 100))
        rmse = np.sqrt(np.mean((pred_mag[valid_mask] - actual_chg_test[valid_mask] * 100)**2))
    else:
        mae = rmse = None

    return {
        'horizon': horizon_name,
        'n_all': len(preds),
        'n_valid': n_valid,
        'n_excluded': n_excluded,
        'threshold': threshold,
        'acc_all': acc_all,
        'acc_valid': acc_valid,
        'auc_all': auc_all,
        'auc_valid': auc_valid,
        'latest_cal': float(cal_probs[-1]),
        'latest_pred_mag': float(pred_mag[-1]),
        'preds': preds,
        'cal_probs': cal_probs,
        'y_true': y_true,
        'actual_chg': actual_chg_test,
        'valid_mask': valid_mask,
        'bucket_acc': bucket_acc,
        'pred_mag': pred_mag,
        'mae': mae,
        'rmse': rmse,
        'feats': feats,
    }

print("\n" + "=" * 75)
print("  三窗口贝叶斯集成 + 涨跌幅度预测")
print(f"  评估规则: 实际|涨跌幅| < 1% 的样本不计入正确率")
print("=" * 75)

results = {}
for h, sel, chg_col in [(1, sel_1d, 'actual_chg_1d'), (3, sel_3d, 'actual_chg_3d')]:
    r = ensemble_predict_with_mag(data, sel, f'label_{h}d', chg_col, f'{h}天后')
    results[h] = r
    print(f"\n[{r['horizon']}]")
    print(f"  全部样本:   {r['n_all']} 个")
    print(f"  有效样本:   {r['n_valid']} 个  (剔除 {r['n_excluded']} 个|涨跌|<1%)")
    print(f"  全样本准确率:     {r['acc_all']:.2%}" + (f"  AUC: {r['auc_all']:.4f}" if r['auc_all'] else ""))
    print(f"  有效样本准确率:   {r['acc_valid']:.2%}" + (f"  AUC: {r['auc_valid']:.4f}" if r['auc_valid'] else ""))
    if r['mae'] is not None:
        print(f"  幅度预测误差:     MAE={r['mae']:.2f}%  RMSE={r['rmse']:.2f}%")

    print(f"\n  分桶预测准确率 (仅有效样本):")
    for bk, (correct, total, acc) in r['bucket_acc'].items():
        if acc is not None:
            bar = "█" * int(acc * 20)
            print(f"    {bk:<16} 命中 {correct}/{total}  {acc:.1%}  {bar}")
        else:
            print(f"    {bk:<16} 无样本")

# ============================================================
# 6. Beta共轭先验 + 最终预测
# ============================================================
print("\n" + "=" * 75)
print("  贝叶斯可信度更新 (Beta共轭先验)")
print("=" * 75)

final = {}
for h in [1, 3]:
    r = results[h]
    preds = r['preds']; y_true = r['y_true']
    valid_mask = r['valid_mask']

    # 用有效样本计算近期命中率 (更公平)
    recent_n = min(30, valid_mask.sum())
    recent_preds = preds[valid_mask][-recent_n:]
    recent_ytrue = y_true[valid_mask][-recent_n:]
    recent_correct = int((recent_preds == recent_ytrue).sum())

    aa, bb = 1 + recent_correct, 1 + recent_n - recent_correct
    cred = aa / (aa + bb)
    ci_lo = beta_dist.ppf(0.025, aa, bb)
    ci_hi = beta_dist.ppf(0.975, aa, bb)

    prior = data[f'label_{h}d'].mean()
    p_model = r['latest_cal']
    p_final = p_model * cred + prior * (1 - cred)

    final[h] = dict(cred=cred, ci_lo=ci_lo, ci_hi=ci_hi, p_model=p_model,
                    p_final=p_final, prior=prior,
                    recent_acc=recent_correct/recent_n if recent_n > 0 else 0,
                    backtest_acc=r['acc_all'],
                    backtest_acc_valid=r['acc_valid'],
                    pred_mag=r['latest_pred_mag'],
                    mae=r['mae'])

    print(f"\n[{r['horizon']}]")
    print(f"  最近{recent_n}次命中(有效样本): {recent_correct}/{recent_n} = "
          f"{recent_correct/recent_n:.1%}" if recent_n > 0 else "")
    print(f"  Beta({aa},{bb}) 可信度: {cred:.1%} (95%CI {ci_lo:.0%}~{ci_hi:.0%})")
    print(f"  模型概率={p_model:.1%}  先验={prior:.1%}  贝叶斯后验={p_final:.1%}")

# ============================================================
# 7. 最终报告
# ============================================================
print("\n" + "=" * 75)
print(f"  >>> 晓鸣股份 (sz.300967) 最终贝叶斯预测报告 <<<")
print(f"  预测基准日: {df['trade_date'].iloc[-1].date()}")
print(f"  评估口径: 实际|涨跌幅| < 1% 不计入正确率")
print("=" * 75)

latest = df.iloc[-1]; prev = df.iloc[-2]
print(f"\n  【最新行情】")
print(f"    收盘: {latest['close']:.2f} ({(latest['close']-prev['close'])/prev['close']*100:+.2f}%)")
print(f"    换手: {latest['turn']:.2f}%  量: {latest['volume']:,.0f}  PE_TTM: {latest['pe_ttm']:.2f}")
r5  = (c[-1]/c[-6]-1)*100; r10 = (c[-1]/c[-11]-1)*100; r20 = (c[-1]/c[-21]-1)*100
print(f"    5/10/20日涨幅: {r5:+.2f}% / {r10:+.2f}% / {r20:+.2f}%")

print(f"\n  【最近10日K线】")
for i in range(-10, 0):
    row = df.iloc[i]
    chg = (row['close']-row['open'])/row['open']*100
    icon = "阳" if row['close']>=row['open'] else "阴"
    print(f"    {row['trade_date'].date()} [{icon}] O:{row['open']:.2f} C:{row['close']:.2f} "
          f"{chg:+.2f}%  V:{row['volume']:,.0f}")

print(f"\n  {'='*60}")
print(f"         ★ 最终贝叶斯预测 (涨跌幅度) ★")
print(f"  {'='*60}")

# 最新预测分桶
def prob_to_bucket(p):
    if p > 0.65: return '大涨(>2%)', '+++', 3
    elif p > 0.55: return '小涨(1~2%)', '++', 2
    elif p >= 0.45: return '横盘/观望', '~', 0
    elif p >= 0.35: return '小跌(-2~-1%)', '--', -2
    else: return '大跌(<-2%)', '---', -3

for h, label in [(1, '明天'), (3, '3天后')]:
    fp = final[h]
    p = fp['p_final']
    direction = "上涨" if p > 0.5 else "下跌"
    bucket, icon, strength_lv = prob_to_bucket(p)
    strength = abs(p - 0.5) * 2

    if strength > 0.30: lvl = "★★★ 强信号"
    elif strength > 0.15: lvl = "★★☆ 中等信号"
    else: lvl = "★☆☆ 弱信号(谨慎)"

    prior_odds = fp['prior']/(1-fp['prior']) if fp['prior']<1 else 1
    post_odds  = fp['p_final']/(1-fp['p_final']) if fp['p_final']<1 and fp['p_final']>0 else 1
    bf = post_odds/prior_odds if prior_odds>0 else 1

    print(f"\n  {label} ({h}d):")
    print(f"    方向:      {icon} {direction}")
    print(f"    分桶预测:   {bucket}")
    print(f"    P(上涨):   {fp['p_final']:.1%}  (强度 {strength:.0%}, {lvl})")
    print(f"    预测幅度:  {fp['pred_mag']:+.2f}%  (模型线性估算, MAE≈{fp['mae']:.2f}%)")
    print(f"    模型可信度: {fp['cred']:.1%}  (95%CI {fp['ci_lo']:.0%}~{fp['ci_hi']:.0%})")
    print(f"    贝叶斯因子: {bf:.2f}x  (>1支持上涨, >3显著, >10极强)")
    print(f"    先验→后验: {fp['prior']:.1%} → {fp['p_final']:.1%} (Δ={fp['p_final']-fp['prior']:+.1%})")
    print(f"    回测全样本:  Acc={fp['backtest_acc']:.1%}")
    print(f"    回测有效样本: Acc={fp['backtest_acc_valid']:.1%} (剔|涨跌|<1%)")

# 综合判断
p1, p3 = final[1]['p_final'], final[3]['p_final']
b1, _, _ = prob_to_bucket(p1)
b3, _, _ = prob_to_bucket(p3)
print(f"\n  {'='*60}")
print(f"  >>> 综合研判 <<<")
print(f"  {'='*60}")
if p1 > 0.55 and p3 > 0.55:
    msg = "短期+中期一致看涨, 偏多"
elif p1 < 0.45 and p3 < 0.45:
    msg = "短期+中期一致看跌, 偏空"
elif p1 > 0.5 and p3 < 0.5:
    msg = "明天可能反弹但中期承压, 震荡偏弱"
elif p1 < 0.5 and p3 > 0.5:
    msg = "明天可能调整但中期看好, 回调是低吸机会"
else:
    msg = "信号矛盾, 建议观望"
print(f"  明天: P(涨)={p1:.1%} → {b1}  |  预测幅度: {final[1]['pred_mag']:+.2f}%")
print(f"  3天后: P(涨)={p3:.1%} → {b3}  |  预测幅度: {final[3]['pred_mag']:+.2f}%")
print(f"  >> {msg}")
print(f"  平均模型可信度: {(final[1]['cred']+final[3]['cred'])/2:.1%}")

print(f"\n  [!] 免责: 仅供学习研究, 不构成投资建议。")
print(f"      贝叶斯模型基于历史规律, 市场存在不可预测风险。\n")
