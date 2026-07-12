# -*- coding: utf-8 -*-
"""全面分析 v1 股性突变埋伏策略"""
import sys, math, json
from pathlib import Path
from collections import defaultdict
from datetime import datetime

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
from script.strategy_divergent_concepts import load_concept_bars, compute_features, bullish_divergence_score

bars_by_code, name_map = load_concept_bars()

# ---- Run v1 exact logic ----
def run_v1():
    recent_days, history_days = 10, 60
    rebalance_days, max_hold_days = 3, 30
    min_hold, buffer_size = 3, 5
    breadth_min = 0.40
    initial_cash = 1_000_000
    start_date = '2023-01-01'

    all_dates = sorted(set(d for bars in bars_by_code.values() for b in bars for d in [b['trade_date']]))
    all_dates = [d for d in all_dates if d >= start_date]

    cash = float(initial_cash)
    positions = {}
    trades = []
    equity_curve = []
    rebalance_counter = 0
    regime_eval_counter = 0

    for di, date in enumerate(all_dates):
        equity = cash
        for code, pos in positions.items():
            cbars = bars_by_code.get(code, [])
            day_bar = next((b for b in cbars if b['trade_date'] == date), None)
            price = day_bar['close'] if day_bar else pos['buy_price']
            equity += pos['shares'] * price
        equity_curve.append({'date': date, 'equity': round(equity, 2)})

        # ---- 大盘股性 ----
        if regime_eval_counter % 10 == 0:
            all_returns_v = []
            breadth_vals_v = []
            for code, bars in bars_by_code.items():
                bars_before = [b for b in bars if b['trade_date'] <= date]
                if len(bars_before) < 80: continue
                closes_v = [b['close'] for b in bars_before[-60:]]
                for i in range(1, len(closes_v)):
                    if closes_v[i-1] > 0:
                        all_returns_v.append((closes_v[i]-closes_v[i-1])/closes_v[i-1])

            if all_returns_v:
                mv = math.sqrt(sum((r - sum(all_returns_v)/len(all_returns_v))**2 for r in all_returns_v)/len(all_returns_v))
            else:
                mv = 0.015

            recent_20 = all_dates[max(0, di-20):di+1]
            for d in recent_20:
                up, total = 0, 0
                for code, bars in bars_by_code.items():
                    day_b = next((b for b in bars if b['trade_date'] == d), None)
                    prev_b = next((b for b in bars if b['trade_date'] < d), None)
                    if day_b and prev_b:
                        total += 1
                        if day_b['close'] > prev_b['close']: up += 1
                if total > 0: breadth_vals_v.append(up/total)
            bs = math.sqrt(sum((b - sum(breadth_vals_v)/len(breadth_vals_v))**2 for b in breadth_vals_v)/len(breadth_vals_v)) if breadth_vals_v else 0.1

            if mv > 0.025 and bs > 0.12:
                regime = 'fast'
                top_n, rebalance, buffer = 8, 2, 4
                recent, history = 7, 40
            else:
                regime = 'normal'
                top_n, rebalance, buffer = 10, 3, 5
                recent, history = 10, 60
        else:
            # Use last params (don't re-evaluate every time)
            pass

        # ---- 广度 ----
        up_c, total_c = 0, 0
        for code, bars in bars_by_code.items():
            day_b = next((b for b in bars if b['trade_date'] == date), None)
            prev_b = next((b for b in bars if b['trade_date'] < date), None)
            if day_b and prev_b:
                total_c += 1
                if day_b['close'] > prev_b['close']: up_c += 1
        breadth = up_c / total_c if total_c > 0 else 0.5

        if rebalance_counter % rebalance != 0:
            rebalance_counter += 1; regime_eval_counter += 1
            for pos in positions.values(): pos['hold_days'] += 1
            continue

        # ---- 评分 ----
        scores = []
        for code, bars in bars_by_code.items():
            bars_before = [b for b in bars if b['trade_date'] <= date]
            if len(bars_before) < history + recent: continue
            recent_bars = bars_before[-recent:]
            hist_bars = bars_before[-recent - history:-recent]
            if len(recent_bars) < recent or len(hist_bars) < history: continue
            ft_r = compute_features(recent_bars)
            ft_h = compute_features(hist_bars)
            if ft_r is None or ft_h is None: continue
            score = bullish_divergence_score(ft_r, ft_h)
            if ft_r['trend_slope_pct'] <= 0 and ft_r['ret_5d'] <= 0:
                if ft_r['max_drawdown'] > ft_h['max_drawdown']: continue
            scores.append((score, code, recent_bars[-1]['close'], ft_r))
        scores.sort(key=lambda x: -x[0])

        if breadth >= breadth_min:
            effective_top = top_n
            target_codes = {code for _, code, _, _ in scores[:top_n]}
            buffered_codes = {code for _, code, _, _ in scores[:top_n + buffer]}
        else:
            effective_top = min(2, top_n)
            target_codes = {code for _, code, _, _ in scores[:effective_top]}
            buffered_codes = target_codes

        # ---- 卖出 ----
        for code in list(positions):
            pos = positions[code]
            cbars = bars_by_code.get(code, [])
            day_bar = next((b for b in cbars if b['trade_date'] == date), None)
            if not day_bar: continue
            sell_price = day_bar['close']
            hold_days = pos['hold_days'] + rebalance
            if hold_days < min_hold:
                pos['hold_days'] = hold_days; continue
            if code not in buffered_codes:
                sell_reason = 'rank_out'
            elif code not in target_codes:
                pos['hold_days'] = hold_days; continue
            elif hold_days >= max_hold_days:
                sell_reason = 'max_hold'
            else:
                pos['hold_days'] = hold_days; continue

            income = pos['shares'] * sell_price
            cost = pos['shares'] * pos['buy_price']
            cash += income
            trades.append({
                'code': code, 'name': name_map.get(code, code),
                'buy_date': pos['buy_date'], 'buy_price': round(pos['buy_price'], 3),
                'sell_date': date, 'sell_price': round(sell_price, 3),
                'shares': pos['shares'],
                'profit': round(income - cost, 2),
                'profit_pct': round((sell_price / pos['buy_price'] - 1) * 100, 2),
                'sell_reason': sell_reason,
                'hold_days': hold_days - rebalance,
                'regime': regime,
            })
            del positions[code]

        # ---- 买入 ----
        if breadth >= breadth_min:
            for score, code, price, ft in scores[:top_n]:
                if code in positions or len(positions) >= effective_top: continue
                slots = max(1, effective_top - len(positions))
                budget = cash / slots
                shares = int(budget // price)
                if shares <= 0: continue
                cash -= shares * price
                positions[code] = {
                    'buy_date': date, 'buy_price': price,
                    'shares': shares, 'hold_days': 0,
                    'buy_reason': f"score={score:.2f}",
                }

        rebalance_counter += 1; regime_eval_counter += 1

    # Final close
    last_date = all_dates[-1]
    for code, pos in positions.items():
        cbars = bars_by_code.get(code, [])
        lb = next((b for b in reversed(cbars) if b['trade_date'] <= last_date), None)
        sp = lb['close'] if lb else pos['buy_price']
        trades.append({
            'code': code, 'name': name_map.get(code, code),
            'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
            'sell_date': last_date, 'sell_price': sp,
            'shares': pos['shares'], 'profit': round(pos['shares']*(sp-pos['buy_price']), 2),
            'profit_pct': round((sp/pos['buy_price']-1)*100, 2), 'sell_reason': '期末',
            'regime': regime, 'hold_days': '?',
        })

    final_eq = equity_curve[-1]['equity']
    closed = [t for t in trades if t['sell_reason'] != '期末']
    wins = [t for t in closed if t['profit'] > 0]
    eqs = [x['equity'] for x in equity_curve]
    peak = eqs[0]; max_dd = 0.0
    for e in eqs:
        if e > peak: peak = e
        dd = (peak-e)/peak if peak>0 else 0
        if dd > max_dd: max_dd = dd
    gross_p = sum(t['profit'] for t in closed if t['profit']>0)
    gross_l = abs(sum(t['profit'] for t in closed if t['profit']<0))
    pf = round(gross_p/gross_l,2) if gross_l>0 else 99

    return {
        'trades': closed, 'equity_curve': equity_curve,
        'summary': {
            'total_return_pct': round((final_eq-initial_cash)/initial_cash*100,2),
            'max_drawdown_pct': round(max_dd*100,2),
            'win_rate_pct': round(len(wins)/max(1,len(closed))*100,1),
            'profit_factor': pf, 'trade_count': len(closed),
            'final_equity': round(final_eq,0),
        }
    }, all_dates


print("运行 v1 策略...")
result, all_dates = run_v1()
closed = result['trades']
s = result['summary']
print(f"完成: {s['trade_count']}笔 | 收益{s['total_return_pct']:+.1f}% | 回撤{s['max_drawdown_pct']:.1f}%")

# ═══════════════════════════════════════════════════════════════
# 分析
# ═══════════════════════════════════════════════════════════════

# 1. 收益曲线
print()
print("=" * 80)
print("  v1 策略全面分析")
print("=" * 80)

# 逐年表现
by_year = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl_sum': 0.0})
for t in closed:
    yr = t['buy_date'][:4]
    by_year[yr]['trades'] += 1
    if t['profit'] > 0: by_year[yr]['wins'] += 1
    by_year[yr]['pnl_sum'] += t['profit_pct']

print()
print(f"### 📅 逐年表现")
print(f"{'年份':<6} {'交易':>5} {'胜率':>7} {'均盈':>8} {'累计盈亏':>10}")
print('-' * 40)
for yr in sorted(by_year):
    d = by_year[yr]
    wr = d['wins']/d['trades']*100
    avg = d['pnl_sum']/d['trades']
    print(f'{yr:<6} {d["trades"]:>5} {wr:>6.1f}% {avg:>+7.2f}% {d["pnl_sum"]:>+9.1f}%')

# 2. 月度表现
by_month = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl_sum': 0.0})
for t in closed:
    ym = t['buy_date'][:7]
    by_month[ym]['trades'] += 1
    if t['profit'] > 0: by_month[ym]['wins'] += 1
    by_month[ym]['pnl_sum'] += t['profit_pct']

monthly_pnl = sorted([(ym, d['pnl_sum'], d['wins']/d['trades']) for ym, d in by_month.items()])
positive_months = sum(1 for _, pnl, _ in monthly_pnl if pnl > 0)
print()
print(f"### 📆 月度胜率")
print(f"  盈利月份: {positive_months}/{len(monthly_pnl)} ({positive_months/len(monthly_pnl)*100:.0f}%)")

# 3. 持股天数分布
hold_days_list = [t['hold_days'] for t in closed if isinstance(t.get('hold_days', '?'), (int, float))]
if hold_days_list:
    avg_hd = sum(hold_days_list)/len(hold_days_list)
    med_hd = sorted(hold_days_list)[len(hold_days_list)//2]
    print()
    print(f"### ⏱ 持股天数")
    print(f"  平均: {avg_hd:.1f}天 | 中位: {med_hd}天")
    buckets = [(1,3),(4,6),(7,10),(11,15),(16,20),(21,30)]
    for lo, hi in buckets:
        bt = [t for t in closed if lo <= t.get('hold_days',0) <= hi]
        if bt:
            wins_b = sum(1 for t in bt if t['profit']>0)
            avg_b = sum(t['profit_pct'] for t in bt)/len(bt)
            bar = '█' * int(len(bt)/max(1,max(len(bt) for bt2 in [([t for t in closed if l2<=t.get('hold_days',0)<=h2]) for l2,h2 in buckets]) )*30)
            buckets_i = [(1,3),(4,6),(7,10),(11,15),(16,20),(21,30)]
            max_cnt = max(len([t for t in closed if l2<=t.get('hold_days',0)<=h2]) for l2,h2 in buckets_i)
            bar = '█' * int(len(bt)/max_cnt*30)
            print(f'  {lo:>2}-{hi:<2}天: {len(bt):>4}笔  胜率{wins_b/len(bt)*100:.0f}%  均盈{avg_b:>+5.1f}%  {bar}')

# 4. 盈亏分布
pnls = [t['profit_pct'] for t in closed]
pnls_sorted = sorted(pnls)
print()
print(f"### 📊 盈亏分布")
print(f"  最大盈利: {max(pnls):+.1f}% | 最大亏损: {min(pnls):+.1f}%")
print(f"  盈利>10%: {sum(1 for p in pnls if p>10)}笔 | 亏损<-10%: {sum(1 for p in pnls if p<-10)}笔")
print(f"  P25: {pnls_sorted[len(pnls)//4]:+.1f}% | P50: {pnls_sorted[len(pnls)//2]:+.1f}% | P75: {pnls_sorted[len(pnls)*3//4]:+.1f}%")

# 5. 卖出后走势
forward_returns = {3:[], 5:[], 10:[]}
premature = 0; good = 0
for t in closed:
    code = t['code']
    sell_date = t['sell_date']
    sell_price = t['sell_price']
    bars = bars_by_code.get(code, [])
    future = [b for b in bars if b['trade_date'] > sell_date]
    for h in [3,5,10]:
        if len(future) >= h:
            fr = (future[h-1]['close']/sell_price - 1)*100
            forward_returns[h].append(fr)
    if len(future) >= 10:
        f10 = (future[9]['close']/sell_price - 1)*100
        if f10 > 5: premature += 1
        elif f10 < -3: good += 1

print()
print(f"### 🚪 卖出质量")
print(f"  总卖出: {len(closed)}笔")
for h in [3,5,10]:
    vals = forward_returns[h]
    if vals:
        avg = sum(vals)/len(vals); med = sorted(vals)[len(vals)//2]
        up_pct = sum(1 for v in vals if v>0)/len(vals)*100
        print(f"  卖后{h:>2}天: 均{avg:>+6.2f}%  中位{med:>+6.2f}%  继续涨{up_pct:.0f}%")
print(f"  提前下车(>5%): {premature}笔 ({premature/max(1,len(closed))*100:.0f}%)")
print(f"  正确卖出(<-3%): {good}笔 ({good/max(1,len(closed))*100:.0f}%)")

# 6. Regime 分析
fast_trades = [t for t in closed if t.get('regime')=='fast']
normal_trades = [t for t in closed if t.get('regime')=='normal']
print()
print(f"### 🎯 Regime 对比")
for label, bt in [('fast', fast_trades), ('normal', normal_trades)]:
    if bt:
        wr = sum(1 for t in bt if t['profit']>0)/len(bt)*100
        avg = sum(t['profit_pct'] for t in bt)/len(bt)
        avg_h = sum(t.get('hold_days',0) for t in bt)/len(bt)
        print(f"  {label:<8}: {len(bt):>4}笔  胜率{wr:.1f}%  均盈{avg:+.2f}%  均持{avg_h:.1f}天")

# 7. 最赚钱/最亏钱的概念
by_concept = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl_sum': 0.0})
for t in closed:
    c = t['name']
    by_concept[c]['trades'] += 1
    if t['profit'] > 0: by_concept[c]['wins'] += 1
    by_concept[c]['pnl_sum'] += t['profit_pct']

print()
print(f"### 🏆 最佳概念 (交易≥5笔)")
best = [(n, d) for n, d in by_concept.items() if d['trades'] >= 5]
best.sort(key=lambda x: -x[1]['pnl_sum'])
for n, d in best[:10]:
    print(f"  {n:<20} {d['trades']:>3}笔  胜率{d['wins']/d['trades']*100:.0f}%  累计{d['pnl_sum']:+.1f}%")

print()
print(f"### 👎 最差概念 (交易≥5笔)")
worst = sorted(best, key=lambda x: x[1]['pnl_sum'])
for n, d in worst[:10]:
    print(f"  {n:<20} {d['trades']:>3}笔  胜率{d['wins']/d['trades']*100:.0f}%  累计{d['pnl_sum']:+.1f}%")

# 8. 最大回撤期间
print()
print(f"### 📉 最大回撤分析")
eqs = result['equity_curve']
peak_val = eqs[0]['equity']; peak_date = eqs[0]['date']
dd_start = ''; dd_end = ''; max_dd_val = 0
for e in eqs:
    if e['equity'] > peak_val:
        peak_val = e['equity']; peak_date = e['date']
    dd = (peak_val - e['equity'])/peak_val if peak_val>0 else 0
    if dd > max_dd_val:
        max_dd_val = dd
        dd_start = peak_date; dd_end = e['date']
print(f"  最大回撤: {max_dd_val*100:.1f}%")
print(f"  从 {dd_start} 峰值 → {dd_end} 谷底")
