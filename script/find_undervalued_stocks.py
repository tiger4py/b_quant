# -*- coding: utf-8 -*-
"""
找出概念內「低估 + 回歸潛力」的股票。
邏輯：股價在自身歷史低檔 + 歷史上跟概念走 + 近期脫鉤 → 有回歸補漲空間
"""
import sqlite3, math, os, csv, glob, sys
from collections import defaultdict

db = sqlite3.connect('data/stock.db')

concept_codes = {'308822': '重组蛋白', '309081': '减肥药'}

# Load concept bars
csv_dir = 'data/concept'
csv_files = glob.glob(os.path.join(csv_dir, '*', '*.csv'))
concept_bars = defaultdict(list)
for fp in csv_files:
    with open(fp, 'r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            code = row.get('concept_code', '')
            if code in concept_codes:
                concept_bars[code].append({
                    'date': row['trade_date'],
                    'close': float(row['close']) if row.get('close') else None,
                })
for code in concept_bars:
    concept_bars[code].sort(key=lambda b: b['date'])

# Stock lists
concept_stocks = {
    '308822': ['sh.600223','sh.600351','sh.600420','sh.600535','sh.600566','sh.600645','sh.600812','sh.600867','sh.603707','sh.603983','sh.688062','sh.688068','sh.688105','sh.688131','sh.688136','sh.688137','sh.688166','sh.688179','sh.688222','sh.688238','sh.688253','sh.688266','sh.688293','sh.688336','sh.688363','sh.688520','sh.688553','sh.688687','sh.688739','sh.688765','sz.000513','sz.000813','sz.002007','sz.002252','sz.002584','sz.300122','sz.300149','sz.300204','sz.300289','sz.300485','sz.300558','sz.300653','sz.300683','sz.300723','sz.300896','sz.301047','sz.301080','sz.301087','sz.301108','sz.301166','sz.301290','sz.301371'],
    '309081': ['sh.600062','sh.600129','sh.600196','sh.600267','sh.600276','sh.600380','sh.600557','sh.600721','sh.600867','sh.603087','sh.603538','sh.603707','sh.605116','sh.688105','sh.688117','sh.688131','sh.688136','sh.688166','sh.688202','sh.688222','sh.688321','sh.688550','sh.688602','sh.688621','sh.688690','sh.688759','sz.000411','sz.000513','sz.000739','sz.000813','sz.000935','sz.000950','sz.000963','sz.002038','sz.002099','sz.002294','sz.002317','sz.002742','sz.002821','sz.002907','sz.300003','sz.300122','sz.300199','sz.300255','sz.300363','sz.300404','sz.300487','sz.300723','sz.300765','sz.300896','sz.301092','sz.301166','sz.301257','sz.301263','sz.301277','sz.301363','sz.301393','sz.301509','sz.301520']
}

def calc_corr(x, y):
    m = len(x)
    if m < 5: return 0
    mx = sum(x)/m; my = sum(y)/m
    cx = sum((a-mx)*(b-my) for a,b in zip(x,y))/m
    sx = math.sqrt(sum((a-mx)**2 for a in x)/m)
    sy = math.sqrt(sum((b-my)**2 for b in y)/m)
    return cx/(sx*sy) if sx*sy>0 else 0

def analyze(concept_code, stock_codes):
    concept_name = concept_codes[concept_code]
    bars_c = concept_bars[concept_code]
    concept_closes = {b['date']: b['close'] for b in bars_c if b['close']}
    all_dates = sorted(concept_closes.keys())

    recent_dates = [d for d in all_dates if d >= '2026-05-28']
    if recent_dates and concept_closes.get(recent_dates[0]) and concept_closes.get(recent_dates[-1]):
        concept_ret_30d = (concept_closes[recent_dates[-1]] - concept_closes[recent_dates[0]]) / concept_closes[recent_dates[0]] * 100
    else:
        concept_ret_30d = 0

    # Concept 250d range
    dates_250 = [d for d in all_dates if d >= '2025-07-01']
    c_vals_250 = [concept_closes[d] for d in dates_250 if d in concept_closes]
    c_high_250 = max(c_vals_250) if c_vals_250 else 1
    c_low_250 = min(c_vals_250) if c_vals_250 else 0
    concept_pos_250 = (concept_closes[all_dates[-1]] - c_low_250) / (c_high_250 - c_low_250) * 100 if c_high_250 > c_low_250 else 50

    results = []
    for code in stock_codes:
        cur = db.execute('''
            SELECT trade_date, close, volume, high, low, pe_ttm, turn
            FROM stock_daily WHERE code = ? AND trade_date >= '2025-07-01'
            ORDER BY trade_date
        ''', (code,))
        rows = cur.fetchall()
        if len(rows) < 50: continue

        dates = [r[0] for r in rows]
        closes = [r[1] for r in rows]
        volumes = [r[2] for r in rows]
        highs = [r[3] for r in rows]
        lows = [r[4] for r in rows]
        pe_ttms = [r[5] for r in rows if r[5] and r[5] > 0]
        turns = [r[6] for r in rows if r[6] and r[6] > 0]

        cur_price = closes[-1]
        high_250 = max(highs)
        low_250 = min(lows)
        pct_from_high_250 = (cur_price - high_250) / high_250 * 100
        pct_from_low_250 = (cur_price - low_250) / low_250 * 100
        range_position = (cur_price - low_250) / (high_250 - low_250) * 100 if high_250 > low_250 else 50

        # Aligned returns with concept
        stock_close_dict = dict(zip(dates, closes))
        concept_returns, stock_returns = [], []
        for i in range(1, len(all_dates)):
            d, dp = all_dates[i], all_dates[i-1]
            if d in concept_closes and dp in concept_closes and d in stock_close_dict and dp in stock_close_dict:
                cr = (concept_closes[d] - concept_closes[dp]) / concept_closes[dp]
                sr = (stock_close_dict[d] - stock_close_dict[dp]) / stock_close_dict[dp]
                concept_returns.append(cr)
                stock_returns.append(sr)

        if len(stock_returns) < 30: continue

        n = len(stock_returns)
        mean_c = sum(concept_returns)/n; mean_s = sum(stock_returns)/n
        cov = sum((concept_returns[i]-mean_c)*(stock_returns[i]-mean_s) for i in range(n))/n
        std_c = math.sqrt(sum((r-mean_c)**2 for r in concept_returns)/n)
        std_s = math.sqrt(sum((r-mean_s)**2 for r in stock_returns)/n)
        corr_full = cov/(std_c*std_s) if std_c*std_s > 0 else 0
        beta = cov/(std_c*std_c) if std_c > 0 else 1.0

        split = max(n - 30, 20)
        corr_hist = calc_corr(concept_returns[:split], stock_returns[:split])
        corr_recent = calc_corr(concept_returns[split:], stock_returns[split:])

        # Stock 30d return
        stock_recent = [d for d in dates if d >= '2026-05-28']
        if len(stock_recent) >= 2:
            idx0 = dates.index(stock_recent[0]); idx1 = dates.index(stock_recent[-1])
            stock_ret_30d = (closes[idx1] - closes[idx0]) / closes[idx0] * 100
        else:
            stock_ret_30d = 0

        perf_div = concept_ret_30d - stock_ret_30d

        # PE
        avg_pe = sum(pe_ttms)/len(pe_ttms) if pe_ttms else 0
        cur_pe = pe_ttms[-1] if pe_ttms else 0
        pe_percentile = sum(1 for p in pe_ttms if p < cur_pe)/len(pe_ttms)*100 if pe_ttms else 50

        # Volume
        volumes_clean = [v if v else 0 for v in volumes]
        if len(volumes_clean) >= 60:
            vol_10d = sum(volumes_clean[-10:])/10
            vol_60d = sum(volumes_clean[-60:-10])/max(1, len(volumes_clean[-60:-10]))
            vol_ratio = vol_10d/vol_60d if vol_60d > 0 else 1
        elif len(volumes_clean) >= 10:
            vol_ratio = sum(volumes_clean[-5:])/max(1,sum(volumes_clean[:5]))*len(volumes_clean)/5
        else:
            vol_ratio = 1

        # ===== COMPOSITE SCORE =====
        # High score = more undervalued + higher reversion potential
        score = (
            (100 - range_position) * 0.30 +    # Near 250d low (biggest weight)
            corr_full * 25 +                     # Historically tracks concept
            max(0, corr_hist - corr_recent) * 20 +  # Recent decoupling → will revert
            max(0, perf_div) * 1.0 +             # Stock lagging concept
            (100 - pe_percentile) * 0.08 +       # PE low
            (min(vol_ratio, 3) - 0.5) * 3 +      # Volume signal
            beta * 5                              # High beta → more responsive
        )

        # Name
        cur2 = db.execute('SELECT name FROM stock_info WHERE code=?', (code,))
        name_row = cur2.fetchone()
        name = name_row[0] if name_row else code

        results.append({
            'code': code, 'name': name, 'cur_price': cur_price,
            'range_pos': range_position, 'pct_from_low_250': pct_from_low_250,
            'pct_from_high_250': pct_from_high_250,
            'corr_full': corr_full, 'corr_hist': corr_hist, 'corr_recent': corr_recent,
            'corr_drop': corr_hist - corr_recent,
            'stock_ret_30d': stock_ret_30d, 'concept_ret_30d': concept_ret_30d,
            'perf_div': perf_div, 'beta': beta,
            'pe_percentile': pe_percentile, 'cur_pe': cur_pe, 'avg_pe': avg_pe,
            'vol_ratio': vol_ratio, 'score': score,
        })

    results.sort(key=lambda x: -x['score'])
    return results, concept_ret_30d, concept_pos_250


# ============ RUN ============
print('=' * 105)
print('  概念內「低估 + 回歸潛力」分析 — 重組蛋白 & 減肥藥')
print('  邏輯: 股價在歷史低檔 + 歷史高度跟隨概念 + 近期脫鉤 → 具備補漲回歸空間')
print('=' * 105)

for cc in ['308822', '309081']:
    results, concept_ret, concept_pos = analyze(cc, concept_stocks[cc])
    name = concept_codes[cc]

    print(f'\n{"━"*105}')
    print(f'  【{name}】 概念指數近30日: {concept_ret:+.1f}%  |  概念250日位置: {concept_pos:.0f}%')
    print(f'{"━"*105}')
    hdr = (f'{"排名":<4} {"股票":<14} {"現價":>7} {"250位":>6} {"距低":>7} {"距高":>7} '
           f'{"歷史相關":>8} {"近期相關":>8} {"脫鉤":>6} {"落後概念":>8} {"PE分位":>6} {"量比":>5} {"Beta":>5}')
    print(hdr)
    print('-' * 105)

    for i, r in enumerate(results[:20], 1):
        markers = []
        if r['range_pos'] < 20: markers.append('📍')
        if r['corr_full'] > 0.55 and r['corr_drop'] > 0.1: markers.append('🔗')
        if r['perf_div'] > 5: markers.append('📉')
        if r['vol_ratio'] > 1.5: markers.append('📊')
        mk = ' '.join(markers)

        print(f'{i:<4} {r["name"]:<14} {r["cur_price"]:>7.2f} {r["range_pos"]:>5.0f}% '
              f'{r["pct_from_low_250"]:>+6.1f}% {r["pct_from_high_250"]:>+6.1f}% '
              f'{r["corr_full"]:>8.3f} {r["corr_recent"]:>8.3f} {r["corr_drop"]:>+5.2f} '
              f'{r["perf_div"]:>+7.1f}% {r["pe_percentile"]:>5.0f}% {r["vol_ratio"]:>4.1f}x {r["beta"]:>5.2f}  {mk}')

    # Hidden gems summary
    hidden = [r for r in results if r['range_pos'] < 30 and r['corr_full'] > 0.5 and r['perf_div'] > 0]
    if hidden:
        print(f'\n  💎 重點標的（低檔 + 高相關 + 落後概念 = 回歸潛力最大）:')
        for r in hidden[:8]:
            print(f'     ► {r["name"]:<14} ({r["code"]})  '
                  f'現價{r["cur_price"]:.2f}  '
                  f'歷史低位({r["range_pos"]:.0f}%)  '
                  f'相關性{r["corr_full"]:.2f}(史)→{r["corr_recent"]:.2f}(今)  '
                  f'落後概念{r["perf_div"]:+.1f}%  '
                  f'PE分位{r["pe_percentile"]:.0f}%  '
                  f'Beta={r["beta"]:.2f}')

print()
print('=' * 105)
print('  解讀符號: 📍=歷史低檔  🔗=相關性脫鉤(待回歸)  📉=大幅落後概念  📊=近期放量')
print('  策略邏輯: 概念強 + 個股弱 + 歷史相關高 → 均值回歸 → 補漲空間')
print('=' * 105)

db.close()
