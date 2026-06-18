# -*- coding: utf-8 -*-
"""
晓鸣股份 (sz.300967) 贝叶斯涨跌预测 v4 — 涨跌幅回归版
=====================================================
核心方法:
  1. 50+ 多维特征工程，互信息回归筛选 Top 特征
  2. 三时间窗口贝叶斯集成 (300/500/700天)
  3. BayesianRidge + LinearRegression 双模型集成 × 三窗口 → 预测涨跌幅(%)
  4. Beta 共轭先验: 用近期方向命中率修正置信度
  5. 准确率统计: 排除实际涨跌幅在 ±1% 以内的样本

用法:
    python backtest/strategy/stock/bayes_predict_300967_dpv4.py
"""

import sqlite3
import sys
import os
import warnings
from pathlib import Path

# Windows 终端 UTF-8 编码
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    os.environ['PYTHONIOENCODING'] = 'utf-8'

ROOT_DIR = Path(__file__).resolve().parents[3]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import mutual_info_regression
from sklearn.linear_model import BayesianRidge, LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error
from scipy.stats import beta as beta_dist
warnings.filterwarnings('ignore')

# ============ 可调参数 ============
TOP_K = 40                   # 互信息筛选特征数 (特征扩充后提高)
TRAIN_WINDOWS = (300, 500, 700)  # 三窗口集成
MIN_WINDOW = 300             # 最小训练窗口
SMALL_MOVE_THRESHOLD = 1.0   # 涨跌幅≤1% 不计入准确率统计

# ============ 1. 数据加载 ============

print("[1/5] 加载数据...")
DB_PATH = ROOT_DIR / 'data' / 'stock.db'
conn = sqlite3.connect(str(DB_PATH))
df = pd.read_sql(
    "SELECT trade_date, open, high, low, close, volume, amount, turn, pe_ttm "
    "FROM stock_daily WHERE code='sz.300967' ORDER BY trade_date", conn)
conn.close()
df['trade_date'] = pd.to_datetime(df['trade_date'])
df = df.reset_index(drop=True)
print(f"      数据范围: {df['trade_date'].iloc[0].date()} ~ {df['trade_date'].iloc[-1].date()}"
      f", 共 {len(df)} 个交易日")
print(f"      最新收盘: {df['close'].iloc[-1]:.2f}")
print("=" * 72)

# ============ 2. 特征工程 (50+ 维) ============

print("[2/5] 特征工程 (50+ 维)...")

c = df['close'].values.astype(float)
h = df['high'].values.astype(float)
l = df['low'].values.astype(float)
v = df['volume'].values.astype(float)
a = df['amount'].values.astype(float)
t = df['turn'].values.astype(float)
o = df['open'].values.astype(float)


def spc(arr, p):
    """安全百分比变化"""
    r = np.full(len(arr), np.nan)
    for i in range(p, len(arr)):
        if arr[i - p] != 0:
            r[i] = (arr[i] - arr[i - p]) / abs(arr[i - p])
    return r


def rsma(arr, w):
    return pd.Series(arr).rolling(w).mean().values


def rstd(arr, w):
    return pd.Series(arr).rolling(w).std().values


def rmax(arr, w):
    return pd.Series(arr).rolling(w).max().values


def rmin(arr, w):
    return pd.Series(arr).rolling(w).min().values


def rema(arr, w):
    """指数移动平均"""
    return pd.Series(arr).ewm(span=w, adjust=False).mean().values


def rskew(arr, w):
    """滚动偏度"""
    return pd.Series(arr).rolling(w).skew().values


def rkurt(arr, w):
    """滚动峰度"""
    return pd.Series(arr).rolling(w).kurt().values


def rcorr(a1, a2, w):
    """滚动相关系数"""
    return pd.Series(a1).rolling(w).corr(pd.Series(a2)).values


def rsum(arr, w):
    """滚动求和"""
    return pd.Series(arr).rolling(w).sum().values


# ==================== 特征工程 (100+ 维) ====================

fe = pd.DataFrame()
fe['trade_date'] = df['trade_date']
daily_ret = spc(c, 1)
daily_ret_fill = np.nan_to_num(daily_ret, nan=0.0)
hl_range = np.where(h != l, h - l, 1e-8)

# ======== 1. 动量族 (14个) ========
# 多周期收益率
for p in [1, 2, 3, 5, 7, 10, 14, 20]:
    fe[f'ret_{p}'] = spc(c, p)

# 动量加速度 (二阶导)
fe['mom_accel'] = fe['ret_5'] - fe['ret_20']
fe['mom_accel_2'] = fe['ret_3'] - fe['ret_10']
fe['ret_ratio_5_20'] = fe['ret_5'] / np.where(np.abs(fe['ret_20']) > 0.001, np.abs(fe['ret_20']), 0.01)
# 短期反转信号: 昨日涨今日跌的倾向
fe['reversal_1'] = -daily_ret * np.roll(daily_ret, 1)
# 动量持续性: ret_5 和 ret_10 符号一致
fe['mom_consist'] = ((fe['ret_5'] > 0).astype(float) == (fe['ret_10'] > 0).astype(float)).astype(float)

# ======== 2. 波动率族 (16个) ========
# 多周期历史波动率
for w in [5, 10, 20, 60]:
    fe[f'vstd_{w}'] = rstd(daily_ret, w)

# ATR (原始版 + 百分比版)
tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
atr_raw = rsma(tr, 14)
fe['atr'] = atr_raw / np.where(c != 0, c, 1e-8)
fe['atr_chg'] = spc(atr_raw, 5)  # ATR变化率

# Parkinson 波动率 (用 OHLC 比纯收盘更高效)
parkinson = np.sqrt((np.log(h / np.where(l > 0, l, 1e-8)) ** 2) / (4 * np.log(2)))
fe['park_vol_10'] = rsma(parkinson, 10)
fe['park_vol_20'] = rsma(parkinson, 20)

# Garman-Klass 波动率 (更精确的 OHLC 估计量)
gk = np.sqrt(0.5 * np.log(h / np.where(l > 0, l, 1e-8)) ** 2
             - (2 * np.log(2) - 1) * np.log(c / np.where(o > 0, o, 1e-8)) ** 2)
fe['gk_vol_10'] = rsma(np.nan_to_num(gk, nan=0), 10)
fe['gk_vol_20'] = rsma(np.nan_to_num(gk, nan=0), 20)

# 波动率变化率
fe['vol_roc_5'] = spc(fe['vstd_10'].values, 5)
fe['vol_exp'] = fe['vstd_5'] / np.where(fe['vstd_20'] > 0.001, fe['vstd_20'], 0.01)
fe['vol_of_vol'] = rstd(fe['vstd_5'].values, 20)  # 波动率的波动率
fe['vol_regime'] = fe['vstd_5'] - fe['vstd_60']   # 当前波动率 vs 长期均值

# 日振幅
fe['day_range'] = (h - l) / np.where(c != 0, c, 1e-8)
fe['day_range_ma5'] = rsma(fe['day_range'].values, 5)

# ======== 3. 不对称波动率 + 尾部风险 (10个) ========
up = np.where(daily_ret > 0, daily_ret, 0)
dn = np.where(daily_ret < 0, abs(daily_ret), 0)
for w in [5, 10, 20]:
    fe[f'up_vol_{w}'] = rsma(up, w)
    fe[f'dn_vol_{w}'] = rsma(dn, w)
# 波动率不对称: >1 表示上涨波动大于下跌
fe['vol_asym_10'] = fe['up_vol_10'] / np.where(fe['dn_vol_10'] > 0.001, fe['dn_vol_10'], 0.01)
fe['vol_asym_20'] = fe['up_vol_20'] / np.where(fe['dn_vol_20'] > 0.001, fe['dn_vol_20'], 0.01)
# 下行偏差 (Sortino 用)
fe['down_std_20'] = rstd(dn, 20)
# 尾部风险: 20日最差5%收益的平均值
fe['tail_risk_20'] = pd.Series(daily_ret).rolling(20).quantile(0.05).values

# ======== 4. 量价关系族 (18个) ========
# 成交量变化
for p in [1, 3, 5, 10]:
    fe[f'vchg_{p}'] = spc(v, p)
for p in [1, 5]:
    fe[f'achg_{p}'] = spc(a, p)

# 相对成交量 (多周期)
for w in [5, 10, 20, 60]:
    vma = rsma(v, w)
    fe[f'vol_ratio_{w}'] = v / np.where(vma > 0, vma, np.nan)

fe['vol_surge'] = (fe['vol_ratio_20'] > 1.5).astype(float)
fe['vol_shrink'] = (fe['vol_ratio_20'] < 0.5).astype(float)
fe['vol_dry'] = (fe['vol_ratio_20'] < 0.35).astype(float)

# OBV 简化版 + 变化率
obv = np.zeros(len(c))
for i in range(1, len(c)):
    if c[i] > c[i - 1]:
        obv[i] = obv[i - 1] + v[i]
    elif c[i] < c[i - 1]:
        obv[i] = obv[i - 1] - v[i]
    else:
        obv[i] = obv[i - 1]
fe['obv_chg_5'] = spc(obv, 5)
fe['obv_chg_10'] = spc(obv, 10)

# 量价背离: 价格上涨但量缩 = 看跌信号
fe['vol_div_5'] = fe['ret_5'] - fe['vchg_5']  # 价升量缩→负值
fe['vol_div_10'] = fe['ret_10'] - fe['vchg_10']

# 均价 (amount/volume 的代理)
avg_price = a / np.where(v > 0, v, 1e-8)
fe['avg_price_chg'] = spc(avg_price, 1)

# 金额量比
fe['amt_ratio_20'] = a / np.where(rsma(a, 20) > 0, rsma(a, 20), np.nan)

# ======== 5. 换手率族 (8个) ========
fe['turn_chg'] = spc(t, 1)
for w in [3, 5, 10, 20]:
    fe[f'turn{w}'] = rsma(t, w)
fe['turn_accel'] = (fe['turn5'] - fe['turn10']) / np.where(fe['turn10'] > 0.001, fe['turn10'], 0.01)
# 换手率异动: 当前 vs 60日均值
fe['turn_abnormal'] = t / np.where(rsma(t, 60) > 0.001, rsma(t, 60), 0.01)
fe['turn_std_10'] = rstd(t, 10)

# ======== 6. K线形态族 (16个) ========
fe['up_shadow'] = (h - np.maximum(o, c)) / hl_range
fe['lo_shadow'] = (np.minimum(o, c) - l) / hl_range
fe['body'] = abs(c - o) / hl_range
fe['is_red'] = (c > o).astype(float)
fe['is_green'] = (c < o).astype(float)
fe['doji'] = (fe['body'] < 0.1).astype(float)
fe['hammer'] = ((fe['lo_shadow'] > 0.6) & (fe['up_shadow'] < 0.1)).astype(float)
fe['shooting_star'] = ((fe['up_shadow'] > 0.6) & (fe['lo_shadow'] < 0.1) & (c < o)).astype(float)
fe['marubozu'] = (fe['body'] > 0.8).astype(float)
fe['close_str'] = (c - l) / hl_range  # 收盘在日内的位置

# 跳空
gap_raw = (o - np.roll(c, 1)) / np.where(np.roll(c, 1) != 0, abs(np.roll(c, 1)), 1e-8)
fe['gap'] = np.where(np.abs(gap_raw) < 0.5, gap_raw, np.nan)
fe['gap_up'] = (gap_raw > 0.02).astype(float)
fe['gap_dn'] = (gap_raw < -0.02).astype(float)
# 跳空后回补
fe['gap_fill'] = np.where(fe['gap_up'].values == 1,
                           (np.minimum(o, c) - np.roll(c, 1)) / np.where(np.roll(c, 1) != 0, abs(np.roll(c, 1)), 1e-8),
                           np.where(fe['gap_dn'].values == 1,
                                    (np.maximum(o, c) - np.roll(c, 1)) / np.where(np.roll(c, 1) != 0, abs(np.roll(c, 1)), 1e-8),
                                    np.nan))

# K线实体 vs ATR (标准化实体大小)
fe['body_atr'] = abs(c - o) / np.where(atr_raw > 0, atr_raw, 1e-8)

# 连续同色K线 (3日)
red3 = ((c > o) & (np.roll(c, 1) > np.roll(o, 1)) & (np.roll(c, 2) > np.roll(o, 2))).astype(float)
green3 = ((c < o) & (np.roll(c, 1) < np.roll(o, 1)) & (np.roll(c, 2) < np.roll(o, 2))).astype(float)
fe['consec_3red'] = red3
fe['consec_3green'] = green3

# ======== 7. 均线偏离族 (12个) ========
for w in [5, 10, 20, 60]:
    ma = rsma(c, w)
    fe[f'bias_{w}'] = (c - ma) / np.where(ma != 0, ma, 1e-8)

ma5, ma10, ma20, ma60 = rsma(c, 5), rsma(c, 10), rsma(c, 20), rsma(c, 60)
fe['ma_slp_5'] = spc(ma5, 5)
fe['ma_slp_10'] = spc(ma10, 10)
fe['ma_slp_20'] = spc(ma20, 20)

# 均线排列: 多头排列=1, 空头=0
fe['ma_align_short'] = ((ma5 > ma10).astype(float) + (ma10 > ma20).astype(float)) / 2.0
fe['ma_align_full'] = ((ma5 > ma10).astype(float) + (ma10 > ma20).astype(float)
                        + (ma20 > ma60).astype(float)) / 3.0
# 均线收敛/发散: MA间距离
fe['ma_spread_5_20'] = (ma5 - ma20) / np.where(ma20 > 0, ma20, 1e-8)
fe['ma_spread_10_60'] = (ma10 - ma60) / np.where(ma60 > 0, ma60, 1e-8)

# ======== 8. 价格位置族 (12个) ========
for w in [5, 10, 20, 60]:
    rh, rl = rmax(c, w), rmin(c, w)
    fe[f'pos_{w}'] = (c - rl) / np.where(rh != rl, rh - rl, 1e-8)

# 回撤
fe['dd_20'] = c / np.where(rmax(c, 20) > 0, rmax(c, 20), 1e-8) - 1
fe['dd_60'] = c / np.where(rmax(c, 60) > 0, rmax(c, 60), 1e-8) - 1

# Donchian 通道位置
donch_hi_20, donch_lo_20 = rmax(c, 20), rmin(c, 20)
fe['donch_pos'] = (c - donch_lo_20) / np.where(donch_hi_20 != donch_lo_20, donch_hi_20 - donch_lo_20, 1e-8)
fe['donch_width'] = (donch_hi_20 - donch_lo_20) / np.where(c != 0, c, 1e-8)

# 创新高/新低
fe['new_high_20'] = (c >= donch_hi_20).astype(float)
fe['new_low_20'] = (c <= donch_lo_20).astype(float)

# 距60日高点天数 (归一化)
days_since_hi = np.zeros(len(c))
last_hi_idx = 0
for i in range(len(c)):
    if c[i] >= rmax(c, 1)[i] and i > 0 and c[i] >= c[i - 1]:
        last_hi_idx = i
    days_since_hi[i] = (i - last_hi_idx) / 60.0
fe['days_since_hi'] = np.clip(days_since_hi, 0, 1.5)

# ======== 9. RSI + 布林带族 (6个) ========
def rsi(close, p):
    delta = np.diff(close, prepend=close[0])
    g = np.where(delta > 0, delta, 0)
    ls = np.where(delta < 0, -delta, 0)
    ag, al = rsma(g, p), rsma(ls, p)
    rs = ag / np.where(al > 0, al, 1e-8)
    return 100 - 100 / (1 + rs)

for p in [5, 6, 14, 24]:
    fe[f'rsi_{p}'] = rsi(c, p)

# 布林带
bb_std = rstd(c, 20)
bb_mid = ma20
fe['bb_pos'] = (c - (bb_mid - 2 * bb_std)) / np.where(4 * bb_std > 0.001, 4 * bb_std, 0.01)
fe['bb_width'] = (4 * bb_std) / np.where(bb_mid > 0, bb_mid, 1e-8)  # 带宽

# ======== 10. 趋势强度 + 效率 (6个) ========
# 趋势效率: 净位移 / 总路径长度 (Kaufman efficiency ratio)
for w in [10, 20]:
    net_change = np.abs(spc(c, w))
    path = rsum(np.abs(daily_ret_fill), w)
    fe[f'eff_ratio_{w}'] = np.abs(spc(c, w)) / np.where(path > 0.001, path, 0.01)

# 简易ADX代理: 方向性波动 / 总波动
fe['adx_proxy_14'] = np.abs(fe['bias_20']) / np.where(fe['vstd_20'] > 0.001, fe['vstd_20'], 0.01)

# 连涨连跌
consec_list = []
cnt = 0
for i in range(len(c)):
    if i == 0:
        cnt = 0
    elif c[i] > c[i - 1]:
        cnt = max(cnt + 1, 1) if cnt >= 0 else 1
    elif c[i] < c[i - 1]:
        cnt = min(cnt - 1, -1) if cnt <= 0 else -1
    else:
        cnt = 0
    consec_list.append(cnt)
fe['consec'] = np.array(consec_list, dtype=float)

# 趋势强度: 连续同向天数归一化
fe['trend_strength'] = np.abs(fe['consec'].values) / 10.0

# ======== 11. 统计特征 (8个) ========
# 收益分布形状
fe['skew_20'] = rskew(daily_ret_fill, 20)
fe['kurt_20'] = rkurt(daily_ret_fill, 20)

# 自相关 (5日收益与滞后5日收益的相关性)
fe['autocorr_5'] = rcorr(daily_ret_fill, np.roll(daily_ret_fill, 5), 20)

# 收益稳定性: 日均收益 / 日收益标准差 (类似 Sharpe 代理)
fe['ret_stability_20'] = rsma(daily_ret_fill, 20) / np.where(fe['vstd_20'] > 0.001, fe['vstd_20'], 1e-8)

# 涨跌比
up_days_20 = rsum((daily_ret_fill > 0).astype(float), 20)
fe['up_ratio_20'] = up_days_20 / 20.0

# 最大单日涨幅/跌幅 (20日)
fe['max_ret_20'] = rmax(daily_ret_fill, 20)
fe['min_ret_20'] = rmin(daily_ret_fill, 20)

# ======== 12. 时间特征 (3个) ========
fe['day_of_week'] = df['trade_date'].dt.dayofweek.values / 4.0  # 0=Mon → 1=Fri
fe['week_of_month'] = (df['trade_date'].dt.day.values - 1) // 7 / 4.0
# 距离季报的可能窗口 (简化为月份正弦)
fe['month_sin'] = np.sin(2 * np.pi * df['trade_date'].dt.month.values / 12)

# -- 标签: 用百分比涨跌幅替代二分类 --

# 1天涨跌幅%: (下日收盘 - 当日收盘) / 当日收盘 * 100
fe['label_1d'] = (np.roll(c, -1) - c) / c * 100
fe['label_3d'] = (np.roll(c, -3) - c) / c * 100
fe['label_5d'] = (np.roll(c, -5) - c) / c * 100
fe['label_7d'] = (np.roll(c, -7) - c) / c * 100
fe['label_10d'] = (np.roll(c, -10) - c) / c * 100
fe['label_14d'] = (np.roll(c, -14) - c) / c * 100
fe['label_21d'] = (np.roll(c, -21) - c) / c * 100

LABEL_COLS = ['label_1d', 'label_3d', 'label_5d', 'label_7d',
              'label_10d', 'label_14d', 'label_21d']
all_feats = [x for x in fe.columns
             if x not in ['trade_date'] + LABEL_COLS]
# 分离特征和标签，处理 NaN
# 特征列 NaN: 前向填充 → 后向填充 → 填0（避免 dropna 损失大量样本）
# 标签列 NaN: 必须丢弃（未来数据未知）
feat_cols = [x for x in all_feats if x in fe.columns]
for col in feat_cols:
    fe[col] = fe[col].ffill().bfill().fillna(0.0)
data = fe.dropna(subset=LABEL_COLS).reset_index(drop=True)
print(f"      有效样本: {len(data)} 行, 原始特征: {len(all_feats)} 个")
for label_col in LABEL_COLS:
    days = label_col.split('_')[1].rstrip('d')
    print(f"      标签 {days}天 涨跌幅: 均值={data[label_col].mean():+.2f}%  "
          f"标准差={data[label_col].std():.2f}%  "
          f"上涨比例={ (data[label_col]>0).mean():.1%}")
print(f"      (±{SMALL_MOVE_THRESHOLD}%内不计入准确率: "
      + "  ".join(f"{lb.split('_')[1].rstrip('d')}={(data[lb].abs()<=SMALL_MOVE_THRESHOLD).mean():.1%}"
                  for lb in LABEL_COLS))
print("=" * 72)

# ============ 3. 互信息回归特征筛选 ============

print("[3/5] 互信息回归特征筛选...")

X_all = data[all_feats].values


def select_features(label_col, top_k=TOP_K):
    """用互信息回归筛选与连续涨跌幅最相关的特征"""
    mi = mutual_info_regression(X_all, data[label_col].values, random_state=42)
    idx = np.argsort(mi)[-top_k:]
    return [all_feats[i] for i in idx], mi


sel_feats = {}  # {horizon_days: (feature_list, mi_array)}
mi_scores = {}
for label_col in LABEL_COLS:
    sel_feats[label_col], mi_scores[label_col] = select_features(label_col)

for label_col in LABEL_COLS:
    days = label_col.split('_')[1].rstrip('d')
    mi = mi_scores[label_col]
    print(f"\n  [{days}天预测] Top-10 信息量特征:")
    for i in np.argsort(mi)[-10:][::-1]:
        print(f"    {all_feats[i]:<22} MI={mi[i]:.4f}")

# ============ 4. 三窗口贝叶斯回归集成 ============

print("\n[4/5] 三窗口贝叶斯回归集成...")
print(f"      模型: BayesianRidge + LinearRegression 双模型 × 三窗口 = 6个预测取中位数")


def bayesian_regression_predict(data, feats, label_col, horizon_name,
                                 windows=TRAIN_WINDOWS,
                                 small_threshold=SMALL_MOVE_THRESHOLD):
    """三时间窗口贝叶斯回归集成预测涨跌幅

    方法:
      - 每个窗口独立训练 BayesianRidge + LinearRegression
      - 三窗口 × 两模型 = 6 个预测取中位数 → 最终涨跌幅预测
      - 方向准确率: 排除 |实际涨跌幅| ≤ 阈值的样本后统计
      - 用近期方向命中率通过 Beta 共轭后验修正置信度

    参数:
        data: 含特征和标签的 DataFrame
        feats: 筛选后的特征列名
        label_col: 标签列名 (连续涨跌幅%)
        horizon_name: 预测周期名
        windows: 训练窗口列表
        small_threshold: 不计入准确率的涨跌幅阈值(%)

    返回:
        result: dict
    """
    X = data[feats].values
    y = data[label_col].values   # 连续涨跌幅%
    n = len(X)
    min_w = min(windows)

    raw_preds = np.full(n - min_w, np.nan)
    y_true = y[min_w:].copy()

    # 每步：三窗口 → 两个模型 → 6个预测
    for i in range(min_w, n):
        x_test = X[i:i + 1]
        step_preds = []
        for w in windows:
            if i - w < 0:
                continue
            Xtr_raw = X[i - w:i]
            ytr = y[i - w:i]
            sc = StandardScaler()
            Xtr = sc.fit_transform(Xtr_raw)
            Xte = sc.transform(x_test)

            # BayesianRidge — 自带不确定性估计
            br = BayesianRidge()
            br.fit(Xtr, ytr)
            step_preds.append(float(br.predict(Xte)[0]))

            # LinearRegression — 稳定基准
            lr = LinearRegression()
            lr.fit(Xtr, ytr)
            step_preds.append(float(lr.predict(Xte)[0]))


        raw_preds[i - min_w] = np.median(step_preds)

    # ------ 简单校准: 用近期残差均值修正 ------
    calib_window = min(150, len(raw_preds) // 3)
    recent_residuals = raw_preds[-calib_window:] - y_true[-calib_window:]
    bias_correction = np.mean(recent_residuals)
    calib_preds = raw_preds - bias_correction

    # ------ 方向判断 ------
    pred_dir = (calib_preds > 0).astype(int)
    true_dir = (y_true > 0).astype(int)

    # ------ 排除小幅波动后计算方向准确率 ------
    big_move_mask = np.abs(y_true) > small_threshold
    filtered_preds = pred_dir[big_move_mask]
    filtered_true = true_dir[big_move_mask]
    n_filtered = len(filtered_true)
    n_total = len(y_true)
    n_excluded = n_total - n_filtered

    if n_filtered > 0:
        direction_acc = np.mean(filtered_preds == filtered_true)
    else:
        direction_acc = 0.5

    # 全样本方向准确率 (做对比)
    full_direction_acc = np.mean(pred_dir == true_dir)

    # ------ 回归误差 ------
    mae = mean_absolute_error(y_true, calib_preds)
    rmse = np.sqrt(mean_squared_error(y_true, calib_preds))

    # 仅大幅波动样本的 MAE
    if n_filtered > 0:
        big_mae = mean_absolute_error(y_true[big_move_mask], calib_preds[big_move_mask])
    else:
        big_mae = mae

    # ------ Beta 共轭后验: 修正方向置信度 ------
    # 用最近N次预测的方向命中率，通过 Beta(α, β) 修正
    lookback = min(50, len(calib_preds))
    recent_pdir = (calib_preds[-lookback:] > 0).astype(int)
    recent_tdir = (y_true[-lookback:] > 0).astype(int)
    recent_acc = np.mean(recent_pdir == recent_tdir) if len(recent_tdir) > 0 else 0.5

    # Beta 先验: 在样本均值附近平坦
    prior_mean = (y > 0).mean()   # 历史上涨比例
    prior_n = 20
    alpha_prior = prior_mean * prior_n
    beta_prior = (1 - prior_mean) * prior_n

    # 似然: 近期方向命中次数
    hits = min(int(recent_acc * lookback), lookback)
    misses = lookback - hits

    # 后验: Beta(α + hits, β + misses) → 方向概率
    alpha_post = alpha_prior + hits
    beta_post = beta_prior + misses
    posterior_dir_p = alpha_post / (alpha_post + beta_post)

    # 后验标准差
    post_std = np.sqrt(alpha_post * beta_post /
                       ((alpha_post + beta_post) ** 2 * (alpha_post + beta_post + 1)))
    lo_p = np.clip(posterior_dir_p - 1.96 * post_std, 0.02, 0.98)
    hi_p = np.clip(posterior_dir_p + 1.96 * post_std, 0.02, 0.98)

    # 贝叶斯因子: 后验 odds / 先验 odds
    def odds(p):
        return p / (1 - p) if p not in (0, 1) else 1.0
    prior_full = (y > 0).mean()
    bf = odds(posterior_dir_p) / odds(prior_full) if odds(prior_full) > 0 else 1.0

    # ------ 最终涨跌幅预测 ------
    final_return_pred = float(calib_preds[-1])

    # 预测值的置信区间: 用近期预测误差的标准差
    pred_error_std = np.std(recent_residuals) if len(recent_residuals) > 0 else 2.0
    pred_lo = final_return_pred - 1.96 * pred_error_std
    pred_hi = final_return_pred + 1.96 * pred_error_std

    return {
        'horizon': horizon_name,
        'features': feats,
        'direction_acc': direction_acc,             # 排除小幅波动后的方向准确率
        'full_direction_acc': full_direction_acc,   # 全样本方向准确率
        'n_filtered': n_filtered,
        'n_excluded': n_excluded,
        'n_total': n_total,
        'mae': mae,
        'rmse': rmse,
        'big_mae': big_mae,
        'prior': prior_full,
        'posterior_dir_prob': posterior_dir_p,      # 后验方向概率
        'prob_interval': (lo_p, hi_p),
        'prob_std': post_std,
        'bayes_factor': bf,
        'recent_acc': recent_acc,
        'alpha_post': alpha_post,
        'beta_post': beta_post,
        'final_return_pred': final_return_pred,     # 预测涨跌幅%
        'pred_interval': (pred_lo, pred_hi),         # 涨跌幅95%区间
        'pred_error_std': pred_error_std,
        'calib_preds': calib_preds,
        'y_true': y_true,
        'bias_correction': bias_correction,
    }


results = {}
HORIZONS = [(1, 'label_1d'), (3, 'label_3d'), (5, 'label_5d'), (7, 'label_7d'),
            (10, 'label_10d'), (14, 'label_14d'), (21, 'label_21d')]
HORIZON_DAYS = [h for h, _ in HORIZONS]
for h, label in HORIZONS:
    sel = sel_feats[label]
    print(f"      训练中: {h}天预测 ({len(sel)} 特征)...")
    results[h] = bayesian_regression_predict(data, sel, label, f'{h}天后')
    res = results[h]
    print(f"        方向准确率(排除±{SMALL_MOVE_THRESHOLD}%): {res['direction_acc']:.1%}  "
          f"({res['n_filtered']}/{res['n_total']}样本, 排除{res['n_excluded']}个)")
    print(f"        全样本方向准确率: {res['full_direction_acc']:.1%}")
    print(f"        MAE: {res['mae']:.2f}%  RMSE: {res['rmse']:.2f}%  "
          f"大幅MAE: {res['big_mae']:.2f}%")
    print(f"        BF: {res['bayes_factor']:.2f}x  "
          f"后验P(涨): {res['posterior_dir_prob']:.1%}")

# ============ 5. 预测报告 ============

print("\n[5/5] 生成预测报告\n")

latest = df.iloc[-1]
prev = df.iloc[-2]
c_arr = df['close'].values

print("=" * 72)
print(f"  晓鸣股份 (sz.300967) 贝叶斯涨跌预测报告 v4")
print(f"  基准日期: {str(latest['trade_date'].date())}")
print("=" * 72)

# ---- 行情概览 ----
chg = (latest['close'] - prev['close']) / prev['close'] * 100
ret5 = (c_arr[-1] / c_arr[-6] - 1) * 100 if len(c_arr) > 5 else 0
ret10 = (c_arr[-1] / c_arr[-11] - 1) * 100 if len(c_arr) > 10 else 0
ret20 = (c_arr[-1] / c_arr[-21] - 1) * 100 if len(c_arr) > 20 else 0

print(f"\n  [行情概览]")
print(f"  收盘: {latest['close']:.2f}  ({'涨' if chg > 0 else '跌'}{chg:+.2f}%)")
print(f"  换手: {latest['turn']:.2f}%  |  成交量: {latest['volume']:,.0f}")
print(f"  5日: {ret5:+.2f}%  |  10日: {ret10:+.2f}%  |  20日: {ret20:+.2f}%")
print(f"  PE_TTM: {latest['pe_ttm']:.2f}")

# ---- 最近10日K线 ----
print(f"\n  [最近10日K线]")
for i in range(-10, 0):
    if abs(i) > len(df):
        continue
    row = df.iloc[i]
    chg_i = (row['close'] - row['open']) / row['open'] * 100
    tag = "+" if row['close'] >= row['open'] else "-"
    print(f"    {str(row['trade_date'].date())}  [{tag}] "
          f"开{row['open']:.2f} 收{row['close']:.2f}  "
          f"日振幅{chg_i:+.2f}%  量{row['volume']:,.0f}")

# ---- 贝叶斯预测结果 ----
print(f"\n  ╔{'═'*66}╗")
print(f"  ║  {'贝 叶 斯 涨 跌 预 测 (涨跌幅版)':^48}║")
print(f"  ╠{'═'*66}╣")

for h in HORIZON_DAYS:
    r = results[h]
    ret_pred = r['final_return_pred']
    dir_prob = r['posterior_dir_prob']
    prior = r['prior']
    lo_p, hi_p = r['prob_interval']
    pred_lo, pred_hi = r['pred_interval']
    direction = "上涨 ↑" if ret_pred > 0 else "下跌 ↓"

    # 信号强度: 基于涨跌幅预测的绝对幅度
    abs_ret = abs(ret_pred)
    if abs_ret > 3:
        level = "★★★ 强信号"
    elif abs_ret > 1.5:
        level = "★★☆ 中等信号"
    else:
        level = "★☆☆ 弱信号"

    print(f"  ║")
    print(f"  ║  ── {h}天后预测 ──")
    print(f"  ║  方向:     {direction}")
    print(f"  ║  预测涨跌幅: {ret_pred:+.2f}%")
    print(f"  ║  涨跌幅区间: [{pred_lo:+.2f}%, {pred_hi:+.2f}%]")
    print(f"  ║  后验P(涨):  {dir_prob:.1%}  (先验 {prior:.1%}, "
          f"Δ={'+'if dir_prob>prior else ''}{dir_prob-prior:+.1%})")
    print(f"  ║  95%概率区间: [{lo_p:.1%}, {hi_p:.1%}]")
    print(f"  ║  贝叶斯因子:  {r['bayes_factor']:.2f}x")
    print(f"  ║  信号强度:    {level}")
    print(f"  ║  ── 历史评估 ──")
    print(f"  ║  方向准确率(排除±{SMALL_MOVE_THRESHOLD}%): {r['direction_acc']:.1%}  "
          f"({r['n_filtered']}/{r['n_total']}样本)")
    print(f"  ║  全样本方向准确率: {r['full_direction_acc']:.1%}")
    print(f"  ║  预测MAE:    {r['mae']:.2f}%  (大幅波动: {r['big_mae']:.2f}%)")
    print(f"  ║  预测RMSE:   {r['rmse']:.2f}%")
    print(f"  ║  近期方向命中率: {r['recent_acc']:.1%} (近50次)")

print(f"  ╚{'═'*66}╝")

# ---- 综合研判 ----
ret1 = results[1]['final_return_pred']
ret3 = results[3]['final_return_pred']
p1 = results[1]['posterior_dir_prob']
p3 = results[3]['posterior_dir_prob']
bf1 = results[1]['bayes_factor']
bf3 = results[3]['bayes_factor']

print(f"\n  [综合研判]")
print(f"  明日: 预测{ret1:+.2f}%  P(涨)={p1:.1%}  BF={bf1:.1f}x")
print(f"  3日 : 预测{ret3:+.2f}%  P(涨)={p3:.1%}  BF={bf3:.1f}x")

# 综合研判: 基于全部周期信号
total_h = len(HORIZON_DAYS)
up_count = sum(1 for h in HORIZON_DAYS if results[h]['final_return_pred'] > 0)
dn_count = total_h - up_count
if up_count >= total_h * 0.7:
    msg = f"一致看涨：{up_count}/{total_h}周期均预测上涨，多头信号明确。"
elif dn_count >= total_h * 0.7:
    msg = f"一致看跌：{dn_count}/{total_h}周期均预测下跌，空头信号明确，建议回避。"
elif up_count > dn_count:
    msg = f"偏多：{up_count}/{total_h}周期看涨，但信号不统一，可轻仓参与。"
elif dn_count > up_count:
    msg = f"偏空：{dn_count}/{total_h}周期看跌，但信号不统一，建议观望。"
else:
    msg = "信号模糊：涨跌预测接近零，建议观望等待更明确信号。"

print(f"  >> {msg}")

# ---- 周期准确率横向对比 ----
print(f"\n  ╔{'═'*66}╗")
print(f"  ║  {'周 期 准 确 率 横 向 对 比 (排除±' + str(SMALL_MOVE_THRESHOLD) + '%)':^44}║")
print(f"  ╠{'═'*66}╣")
print(f"  ║  {'周期':^6} {'方向准确率':^12} {'排除样本':^10} {'MAE':^8} {'RMSE':^8} {'BF':^6} ║")
print(f"  ║  {'─'*6} {'─'*12} {'─'*10} {'─'*8} {'─'*8} {'─'*6} ║")

# 先找出最优周期
best_h = max(HORIZON_DAYS, key=lambda h: results[h]['direction_acc'])
best_acc = results[best_h]['direction_acc']

for h in HORIZON_DAYS:
    r = results[h]
    acc = r['direction_acc']
    mae = r['mae']
    rmse = r['rmse']
    bf = r['bayes_factor']
    excluded = r['n_excluded']
    marker = " ← 最优" if h == best_h else ""
    print(f"  ║  {h:>3}天  {acc:>10.1%}  {excluded:>8}个  {mae:>6.2f}% {rmse:>6.2f}% {bf:>5.1f}x{marker:<5} ║")
print(f"  ╚{'═'*66}╝")
print(f"\n  >>> 方向准确率最高的是 {best_h}天周期，达到 {best_acc:.1%}")

# ---- 关键驱动特征 ----
print(f"\n  [关键驱动因素 - 贝叶斯特征贡献]")
for h, label in HORIZONS:
    sel = sel_feats[label]
    print(f"\n  {h}天预测 Top 特征:")
    X_sel = StandardScaler().fit_transform(data[sel].values)
    # 用线性回归系数作为特征贡献度
    lr = LinearRegression()
    lr.fit(X_sel, data[label].values)
    contribs = []
    for j, fname in enumerate(sel):
        coef = lr.coef_[j] if hasattr(lr.coef_, '__iter__') else lr.coef_
        if not hasattr(coef, '__iter__'):
            coef = [coef]
        c_val = coef[j] if j < len(coef) else coef[0]
        contribs.append((fname, abs(c_val), "看涨" if c_val > 0 else "看跌", c_val))
    contribs.sort(key=lambda x: x[1], reverse=True)
    for fname, d, s, c_val in contribs[:10]:
        bar = "█" * min(int(d * 15), 35)
        print(f"    {fname:<20} coef={c_val:+.4f} {s}  {bar}")

# ---- 预测涨跌幅分布参考 ----
print(f"\n  [历史预测涨跌幅分布参考]")
for h in HORIZON_DAYS:
    r = results[h]
    preds = r['calib_preds']
    actuals = r['y_true']
    bins = [-100, -5, -3, -1, 1, 3, 5, 100]
    labels_bin = ['跌>5%', '跌3~5%', '跌1~3%', '±1%', '涨1~3%', '涨3~5%', '涨>5%']
    print(f"\n  {h}天预测 vs 实际分布:")
    print(f"  {'区间':<10} {'预测次数':>8} {'实际均值':>10} {'方向命中':>10}")
    for j in range(len(bins) - 1):
        mask = (preds > bins[j]) & (preds <= bins[j + 1])
        n_bin = mask.sum()
        if n_bin > 0:
            actual_mean = actuals[mask].mean()
            dir_hit = np.mean((preds[mask] > 0) == (actuals[mask] > 0))
            print(f"  {labels_bin[j]:<10} {n_bin:>8} {actual_mean:>+9.2f}% {dir_hit:>10.1%}")

# ---- 模型可靠性 ----
print(f"\n  [模型可靠性评估]")
for h in HORIZON_DAYS:
    r = results[h]
    acc = r['direction_acc']
    mae = r['mae']
    rmse = r['rmse']
    # 综合评估
    if acc > 0.55 and mae < 2.5:
        grade = "良好 - 预测有参考价值"
    elif acc > 0.50 and mae < 3.5:
        grade = "一般 - 仅供参考"
    else:
        grade = "较弱 - 信号可信度有限"
    print(f"    {h}天模型: 方向准确率(排除±{SMALL_MOVE_THRESHOLD}%)={acc:.1%}  "
          f"MAE={mae:.2f}%  RMSE={rmse:.2f}%  → {grade}")

# ---- 免责 ----
print(f"\n  [!] 免责声明: 仅供学习研究，不构成投资建议。")
print(f"      模型基于历史统计规律，市场存在不可预测风险。")
print(f"      * 准确率统计已排除实际涨跌幅在 ±{SMALL_MOVE_THRESHOLD}% 以内的样本。")
print("=" * 72)
print()
