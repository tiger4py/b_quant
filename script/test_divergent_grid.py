# -*- coding: utf-8 -*-
import sys, math
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
from script.strategy_divergent_concepts import load_concept_bars, compute_features, bullish_divergence_score

bars_by_code, name_map = load_concept_bars()

def run_gated(recent_days=10, history_days=60, top_n=10, rebalance_days=5, max_hold_days=30,
              initial_cash=1_000_000, start_date='2023-01-01', breadth_min=0.4):
    all_dates = sorted(set(d for bars in bars_by_code.values() for b in bars for d in [b['trade_date']]))
    all_dates = [d for d in all_dates if d >= start_date]
    cash = float(initial_cash)
    positions = {}
    trades = []
    equity_curve = []
    rebalance_counter = 0

    for di, date in enumerate(all_dates):
        equity = cash
        for code, pos in positions.items():
            cbars = bars_by_code.get(code, [])
            day_bar = next((b for b in cbars if b['trade_date'] == date), None)
            price = day_bar['close'] if day_bar else pos['buy_price']
            equity += pos['shares'] * price
        equity_curve.append({'date': date, 'equity': round(equity, 2)})

        if rebalance_counter % rebalance_days != 0:
            rebalance_counter += 1
            for pos in positions.values():
                pos['hold_days'] += 1
            continue

        # Market breadth
        up_count = 0; total_count = 0
        for code, bars in bars_by_code.items():
            day_bars = [b for b in bars if b['trade_date'] == date]
            prev_bars = [b for b in bars if b['trade_date'] < date]
            if day_bars and prev_bars:
                total_count += 1
                if day_bars[0]['close'] > prev_bars[-1]['close']:
                    up_count += 1
        breadth = up_count / total_count if total_count > 0 else 0.5

        # Score concepts
        scores = []
        for code, bars in bars_by_code.items():
            bars_before = [b for b in bars if b['trade_date'] <= date]
            if len(bars_before) < history_days + recent_days: continue
            recent_bars = bars_before[-recent_days:]
            hist_bars = bars_before[-recent_days - history_days:-recent_days]
            if len(recent_bars) < recent_days or len(hist_bars) < history_days: continue
            ft_r = compute_features(recent_bars)
            ft_h = compute_features(hist_bars)
            if ft_r is None or ft_h is None: continue
            score = bullish_divergence_score(ft_r, ft_h)
            if ft_r['trend_slope_pct'] <= 0 and ft_r['ret_5d'] <= 0 and ft_r['max_drawdown'] > ft_h['max_drawdown']:
                continue
            scores.append((score, code, recent_bars[-1]['close'], ft_r))
        scores.sort(key=lambda x: -x[0])

        # Gate
        effective_top = top_n if breadth >= breadth_min else min(2, top_n)
        target_codes = {code for _, code, _, _ in scores[:effective_top]}

        # Sell
        for code in list(positions):
            pos = positions[code]
            cbars = bars_by_code.get(code, [])
            day_bar = next((b for b in cbars if b['trade_date'] == date), None)
            if not day_bar: continue
            sell_price = day_bar['close']
            hold_days = pos['hold_days'] + rebalance_days
            sell_reason = None
            if code not in target_codes: sell_reason = 'rank_out'
            elif hold_days >= max_hold_days: sell_reason = 'max_hold'
            if sell_reason is None:
                pos['hold_days'] = hold_days
                continue
            income = pos['shares'] * sell_price
            cash += income
            trades.append({
                'code': code, 'sell_date': date, 'sell_price': sell_price,
                'shares': pos['shares'], 'buy_price': pos['buy_price'],
                'profit': round(income - pos['shares'] * pos['buy_price'], 2),
                'profit_pct': round((sell_price / pos['buy_price'] - 1) * 100, 2),
                'sell_reason': sell_reason,
            })
            del positions[code]

        # Buy (only if breadth OK)
        if breadth >= breadth_min:
            for score, code, price, ft in scores[:effective_top]:
                if code in positions or len(positions) >= effective_top: continue
                slots = max(1, effective_top - len(positions))
                budget = cash / slots
                shares = int(budget // price)
                if shares <= 0: continue
                cash -= shares * price
                positions[code] = {'buy_date': date, 'buy_price': price, 'shares': shares, 'hold_days': 0}

        rebalance_counter += 1

    # Final close
    last_date = all_dates[-1]
    for code, pos in positions.items():
        cbars = bars_by_code.get(code, [])
        lb = next((b for b in reversed(cbars) if b['trade_date'] <= last_date), None)
        sp = lb['close'] if lb else pos['buy_price']
        trades.append({
            'code': code, 'sell_date': last_date, 'sell_price': sp,
            'shares': pos['shares'], 'buy_price': pos['buy_price'],
            'profit': round(pos['shares']*(sp-pos['buy_price']), 2),
            'profit_pct': round((sp/pos['buy_price']-1)*100, 2), 'sell_reason': '期末',
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
    pf = round(gross_p/gross_l, 2) if gross_l>0 else 99

    return {'ret': round((final_eq-initial_cash)/initial_cash*100,2),
            'dd': round(max_dd*100,2), 'wr': round(len(wins)/max(1,len(closed))*100,1),
            'pf': pf, 'trades': len(closed), 'final': round(final_eq,0)}


print(f'{"参数":<40} {"收益":>8} {"回撤":>7} {"胜率":>6} {"PF":>6} {"交易":>6}')
print('-' * 78)

tests = [
    ("基线 t=10 无门控", 10, 60, 10, 5, 30, 0.0),
    ("门控>0.35 t=10", 10, 60, 10, 5, 30, 0.35),
    ("门控>0.40 t=10", 10, 60, 10, 5, 30, 0.40),
    ("门控>0.45 t=10", 10, 60, 10, 5, 30, 0.45),
    ("门控>0.40 t=8", 10, 60, 8, 5, 30, 0.40),
    ("门控>0.40 t=12", 10, 60, 12, 5, 30, 0.40),
    ("门控>0.40 t=8 b=3", 10, 60, 8, 3, 25, 0.40),
    ("门控>0.40 t=10 b=3", 10, 60, 10, 3, 25, 0.40),
]

for label, rd, hd, tn, rb, mh, bm in tests:
    r = run_gated(recent_days=rd, history_days=hd, top_n=tn, rebalance_days=rb, max_hold_days=mh, breadth_min=bm)
    print(f'{label:<40} {r["ret"]:>+7.2f}% {r["dd"]:>6.2f}% {r["wr"]:>5.1f}% {r["pf"]:>5.2f} {r["trades"]:>5}')
