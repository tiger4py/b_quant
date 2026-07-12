# -*- coding: utf-8 -*-
"""比较多种大盘轮动速度的度量方式"""
import sys, math
from pathlib import Path
from collections import defaultdict

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
from script.strategy_divergent_concepts import load_concept_bars

bars_by_code, name_map = load_concept_bars()

all_dates = sorted(set(d for bars in bars_by_code.values() for b in bars for d in [b['trade_date']]))
all_dates = [d for d in all_dates if d >= '2023-01-01']

# Daily data
daily_returns = {}
for date in all_dates:
    daily_returns[date] = {}
    for code, bars in bars_by_code.items():
        day_bar = next((b for b in bars if b['trade_date'] == date), None)
        prev_bar = next((b for b in bars if b['trade_date'] < date), None)
        if day_bar and prev_bar and prev_bar['close'] > 0:
            daily_returns[date][code] = (day_bar['close'] - prev_bar['close']) / prev_bar['close']

print(f'日期: {len(all_dates)} 天, 概念: {len(bars_by_code)}')


def top_n_overlap(date, n=20, lookback=5):
    idx = all_dates.index(date)
    if idx < lookback: return None
    prev_date = all_dates[idx - lookback]
    today_ret = daily_returns.get(date, {})
    prev_ret = daily_returns.get(prev_date, {})
    today_top = set(sorted(today_ret, key=lambda c: today_ret.get(c, -999), reverse=True)[:n])
    prev_top = set(sorted(prev_ret, key=lambda c: prev_ret.get(c, -999), reverse=True)[:n])
    overlap = len(today_top & prev_top) / n
    return 1 - overlap


def rank_correlation(date, lookback=5):
    idx = all_dates.index(date)
    if idx < lookback: return None
    prev_date = all_dates[idx - lookback]
    today_ret = daily_returns.get(date, {})
    prev_ret = daily_returns.get(prev_date, {})
    common = set(today_ret) & set(prev_ret)
    if len(common) < 50: return None
    sorted_t = sorted(common, key=lambda c: today_ret.get(c, -999), reverse=True)
    rank_t = {c: i for i, c in enumerate(sorted_t)}
    sorted_p = sorted(common, key=lambda c: prev_ret.get(c, -999), reverse=True)
    rank_p = {c: i for i, c in enumerate(sorted_p)}
    n = len(common)
    d2_sum = sum((rank_t[c] - rank_p[c])**2 for c in common)
    rho = 1 - (6 * d2_sum) / (n * (n**2 - 1))
    return 1 - rho


def cross_sectional_dispersion(date):
    rets = list(daily_returns.get(date, {}).values())
    if len(rets) < 50: return None
    mean_r = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean_r)**2 for r in rets) / len(rets))


def leader_duration(date, n=5):
    today_top = set(sorted(daily_returns.get(date, {}),
                          key=lambda c: daily_returns[date].get(c, -999), reverse=True)[:n])
    idx = all_dates.index(date)
    durations = []
    for code in today_top:
        dur = 1
        for i in range(idx - 1, max(0, idx - 30), -1):
            prev_date = all_dates[i]
            prev_top = set(sorted(daily_returns.get(prev_date, {}),
                                 key=lambda c: daily_returns[prev_date].get(c, -999), reverse=True)[:n])
            if code in prev_top: dur += 1
            else: break
        durations.append(dur)
    avg_dur = sum(durations) / len(durations) if durations else 1
    return 1 / avg_dur


def avg_autocorr(date):
    acs = []
    for code in list(daily_returns.get(date, {}).keys())[:100]:
        idx = all_dates.index(date)
        rets = []
        for i in range(max(0, idx - 20), idx + 1):
            r = daily_returns.get(all_dates[i], {}).get(code)
            if r is not None: rets.append(r)
        if len(rets) < 7: continue
        mean_r = sum(rets) / len(rets)
        var_r = sum((r - mean_r)**2 for r in rets) / len(rets)
        if var_r < 1e-10: continue
        cov = sum((rets[i] - mean_r)*(rets[i-1] - mean_r) for i in range(1, len(rets))) / (len(rets)-1)
        acs.append(cov / var_r)
    if not acs: return None
    return 1 - abs(sum(acs)/len(acs))


def amount_rank_change(date, lookback=5):
    idx = all_dates.index(date)
    if idx < lookback: return None
    prev_date = all_dates[idx - lookback]
    today_amt, prev_amt = {}, {}
    for code in bars_by_code:
        day_bar = next((b for b in bars_by_code[code] if b['trade_date'] == date), None)
        prev_bar = next((b for b in bars_by_code[code] if b['trade_date'] == prev_date), None)
        if day_bar: today_amt[code] = day_bar.get('amount', 0) or 0
        if prev_bar: prev_amt[code] = prev_bar.get('amount', 0) or 0
    common = set(today_amt) & set(prev_amt)
    if len(common) < 50: return None
    sorted_t = sorted(common, key=lambda c: today_amt[c], reverse=True)
    rank_t = {c: i for i, c in enumerate(sorted_t)}
    sorted_p = sorted(common, key=lambda c: prev_amt[c], reverse=True)
    rank_p = {c: i for i, c in enumerate(sorted_p)}
    changes = [abs(rank_t[c] - rank_p[c]) for c in common]
    return sum(changes) / len(changes) / len(common)


# Sample
sample_dates = all_dates[::20]
print(f'采样: {len(sample_dates)} 个日期')

methods = {
    'Top20重叠率(5d)': lambda d: top_n_overlap(d, 20, 5),
    '排名Spearman(5d)': lambda d: rank_correlation(d, 5),
    '收益离散度': cross_sectional_dispersion,
    '领先持续性(Top5)': lambda d: leader_duration(d, 5),
    '平均自相关(低=快)': avg_autocorr,
    '成交额排名变化': lambda d: amount_rank_change(d, 5),
}

results = {name: [] for name in methods}
for date in sample_dates:
    for name, func in methods.items():
        val = func(date)
        if val is not None:
            results[name].append((date, val))

print()
print(f'{"方法":<22} {"均值":>8} {"中位":>8} {"标准差":>8} {"范围":>15}')
print('-' * 65)
for name, vals in results.items():
    if vals:
        vs = [v[1] for v in vals]
        mean_v = sum(vs)/len(vs)
        med_v = sorted(vs)[len(vs)//2]
        std_v = math.sqrt(sum((v-mean_v)**2 for v in vs)/len(vs))
        print(f'{name:<22} {mean_v:>8.4f} {med_v:>8.4f} {std_v:>8.4f} {min(vs):>7.4f} ~ {max(vs):>7.4f}')

# Correlation
print()
print('各方法之间的相关性:')
print(f'{"":<22}', end='')
names = list(methods.keys())
for n in names:
    print(f'{n[:8]:>8}', end='')
print()
for n1 in names:
    print(f'{n1:<22}', end='')
    for n2 in names:
        d1 = {d: v for d, v in results[n1]}
        d2 = {d: v for d, v in results[n2]}
        common_dates = set(d1) & set(d2)
        if len(common_dates) < 10:
            print(f'   {"-":>6}', end='')
            continue
        x = [d1[d] for d in common_dates]
        y = [d2[d] for d in common_dates]
        mx, my = sum(x)/len(x), sum(y)/len(y)
        sx = math.sqrt(sum((a-mx)**2 for a in x)/len(x))
        sy = math.sqrt(sum((b-my)**2 for b in y)/len(y))
        if sx < 1e-10 or sy < 1e-10:
            print(f'   {"-":>6}', end='')
            continue
        corr = sum((a-mx)*(b-my) for a, b in zip(x, y))/(len(x)*sx*sy)
        print(f'{corr:>8.2f}', end='')
    print()
