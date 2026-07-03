---
name: strategy-review
description: 量化策略评估分析 — 分析→诊断→修复→重跑→对比 闭环。含前向追踪、鱼头鱼身分析、入场时机拆解。
---

# 策略评估分析 Skill（V3 — 含前向追踪 + 鱼头鱼身分析）

对任意回测策略做完整的 **分析→诊断→修复→重跑→对比** 循环。

## 触发条件

用户说"分析策略"、"评估策略"、"诊断策略"、"review strategy"、"帮我看看这个策略"、"为什么这个策略不赚钱"时触发。

## 完整工作流（6 步闭环）

```
快速概览 → 卖出拆解 → 前向追踪 → 鱼头鱼身 → 入场时机 → 根因→修复→重跑
   ↑                                                          │
   └──────────────── 循环直到满意 ←────────────────────────────┘
```

**核心原则：不要只看策略本身的指标，要看单品种前向表现 vs 组合层面表现的差异。两者矛盾时，组合层面说了算。**

---

## 第 1 步：快速概览

从最新 archive JSON 提取关键指标，判断策略是否"活了"：

```python
import json
from pathlib import Path

def quick_overview(strategy_id):
    root = Path('data/strategy') / strategy_id
    month_dirs = sorted([d for d in root.iterdir() if d.is_dir()], 
                        key=lambda d: d.name, reverse=True)
    if not month_dirs:
        return None
    files = sorted(month_dirs[0].glob("*.json"), 
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None
    
    with open(files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    s = data['summary']
    print(f"策略: {data['strategy']['name']}")
    print(f"收益: {s['total_return_pct']:.2f}%  回撤: {s['max_drawdown_pct']:.2f}%")
    print(f"胜率: {s['win_rate_pct']:.1f}%  交易: {s['trade_count']}笔")
    print(f"平均盈利: {s['avg_profit_pct']:.2f}%  盈亏比: {s['profit_factor']}")
    print(f"区间: {data['selection']['start_date']} ~ {data['selection']['end_date']}")
    return data
```

**判断标准**：
- 交易数 = 0 → 策略根本没触发，检查 market_gate 或信号逻辑
- 交易数 > 0 但收益 < 0 → 策略逻辑有问题，进入诊断
- 收益 > 0 但 PF < 1.2 → 靠运气，胜率不够或盈亏比差
- 收益 > 20% 且 PF > 1.5 → 策略有效，可以微调优化
- 收益 > 50% 且 PF > 2.0 且 回撤 < 20% → **优秀，修改前先充分验证，不要轻易破坏**

---

## 第 2 步：卖出原因拆解

```python
from collections import Counter, defaultdict

def analyze_sell_reasons(data):
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    
    def classify(reason):
        if '止损' in reason: return '止损'
        if '移动止盈' in reason or '止盈' in reason: return '移动止盈'
        if '到期' in reason: return '到期'
        if '量价同步' in reason: return '量价同步(散户涌入)'
        return '其他'
    
    groups = {}
    for t in trades:
        cls = classify(t['sell_reason'])
        g = groups.setdefault(cls, {'count': 0, 'wins': 0, 'profit': 0.0, 'pcts': []})
        g['count'] += 1
        g['profit'] += t['profit']
        g['pcts'].append(t['profit_pct'])
        if t['profit'] > 0:
            g['wins'] += 1
    
    print(f"\n{'卖出类型':<16} {'笔数':>4} {'占比':>6} {'胜率':>6} {'累计盈亏':>12} {'平均%':>7}")
    print('-' * 58)
    for cls in groups:
        g = groups[cls]
        print(f"{cls:<16} {g['count']:>4} {g['count']/len(trades)*100:>5.1f}% "
              f"{g['wins']/g['count']*100:>5.0f}% {g['profit']:>12,.0f} "
              f"{sum(g['pcts'])/len(g['pcts']):>6.1f}%")
    
    return groups
```

**关键判断**：
- 某类卖出胜率很低 → 这个卖出条件该改
- 某类卖出占比 > 80% → 策略几乎被这一个条件主导

---

## 第 3 步：前向追踪（⭐最重要）

**这是整个分析中最关键的一步。** 查每笔交易卖出后 1/2/3/4/5/6 个月的价格，判断卖出时机。

```python
import csv
from datetime import datetime

def forward_analysis(data, data_dir='data/etf'):
    """加载全量日线，查每笔卖出后的前向收益"""
    # 1. 加载日线数据
    all_bars = {}
    for year_dir in sorted(Path(data_dir).iterdir()):
        if not year_dir.is_dir(): continue
        for mf in sorted(year_dir.glob('*.csv')):
            try:
                with open(mf, encoding='utf-8-sig') as f:
                    for row in csv.DictReader(f):
                        code = row.get('code','').strip()
                        d = row.get('trade_date','').strip()
                        c = float(row.get('close') or 0)
                        if c > 0: all_bars.setdefault(code, {})[d] = c
            except: pass
    
    all_dates = sorted(set(d for bars in all_bars.values() for d in bars))
    
    def add_months(dt, months):
        m = dt.month + months
        y = dt.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        return datetime(y, m, min(dt.day, 28))
    
    def find_nearest(target_str, dates):
        for d in dates:
            if d >= target_str: return d
        return None
    
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    
    results = []
    for t in trades:
        sell_date = datetime.strptime(t['sell_date'], '%Y-%m-%d')
        code = t['code']
        bars = all_bars.get(code, {})
        if not bars or t['sell_price'] <= 0: continue
        
        code_dates = sorted(bars.keys())
        fwd_returns = []
        for months in [1, 2, 3, 4, 5, 6]:
            target = add_months(sell_date, months)
            nearest = find_nearest(target.strftime('%Y-%m-%d'), code_dates)
            if nearest and nearest in bars:
                fwd_returns.append((bars[nearest] / t['sell_price'] - 1) * 100)
            else:
                fwd_returns.append(None)
        
        results.append({**t, 'fwd': fwd_returns})
    
    # 分类统计
    def classify(reason):
        if '量价同步' in reason: return '量价同步'
        if '到期' in reason: return '到期'
        if '止损' in reason: return '止损'
        return '其他'
    
    by_type = defaultdict(list)
    for r in results:
        by_type[classify(r['sell_reason'])].append(r)
    
    print(f"\n{'卖出类型':<16} {'笔数':>4}", end='')
    for m in range(1, 7): print(f" {'{}月后'.format(m):>9}", end='')
    print(f" {'上涨比':>7}")
    print('-' * 90)
    
    for cls in by_type:
        items = by_type[cls]
        print(f"{cls:<16} {len(items):>4}", end='')
        for m in range(6):
            vals = [r['fwd'][m] for r in items if r['fwd'][m] is not None]
            avg = sum(vals) / len(vals) if vals else 0
            print(f" {avg:>+8.1f}%", end='')
        all_fwd = [r['fwd'][m] for r in items for m in range(6) if r['fwd'][m] is not None]
        pos = sum(1 for v in all_fwd if v > 0)
        print(f" {pos/len(all_fwd)*100:>6.0f}%")
    
    return results
```

**关键判断**：
- 某类卖出后 1 月中位数 > 0 → 卖早了，信号偏早
- 某类卖出后 3 月中位数 > +10% → **卖出是最大漏洞**
- 某类卖出后 1 月中位数 < -2% → 卖出时机合理
- 卖出后创新高占比 > 70% → 趋势根本没走完

### 重要警示

**前向分析显示"卖早了" ≠ 应该改卖出逻辑。** 单品种前向分析只告诉你"这条鱼后面还能长"，但组合层面要考虑：
- 仓位机会成本（占着坑错过其他鱼头）
- 路径损耗（拿全程的震荡回撤）
- 周转速度的复利效应

**如果 PF > 2.5 且前向分析显示卖早了，先不改，继续往下分析。**

---

## 第 4 步：鱼头 vs 鱼身分析（⭐新增）

判断策略到底吃的是鱼的哪个部位，以及吃的量够不够。

```python
def fish_head_vs_body(data, data_dir='data/etf'):
    """比较实际吃到 vs 入场后最高可吃"""
    # ... 加载日线同第3步 ...
    
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    
    results = []
    for t in trades:
        code = t['code']
        bars = all_bars.get(code, {})
        if not bars: continue
        dates = sorted(bars.keys())
        buy_d, sell_d = t['buy_date'], t['sell_date']
        if buy_d not in dates or sell_d not in dates: continue
        
        buy_idx = dates.index(buy_d)
        sell_idx = dates.index(sell_d)
        buy_price = t['buy_price']
        
        # 入场后到卖出后6月的最高价
        post_end = min(len(dates) - 1, sell_idx + 126)
        post_entry_high = max(bars[dates[i]] for i in range(buy_idx, post_end + 1))
        
        fish_head = t['profit_pct']                              # 实际吃到
        fish_body = (post_entry_high / buy_price - 1) * 100      # 入场后最高可吃
        
        results.append({
            'name': t['name'], 'fish_head': fish_head,
            'fish_body': fish_body,
            'ratio': fish_head / fish_body * 100 if fish_body > 0.5 else 0,
        })
    
    heads = sorted([r['fish_head'] for r in results])
    bodies = sorted([r['fish_body'] for r in results])
    ratios = sorted([r['ratio'] for r in results if r['fish_body'] > 0.5])
    
    print(f"\n  N={len(results)}")
    print(f"  鱼头(吃到) 中位数:     {heads[len(heads)//2]:+.1f}%")
    print(f"  鱼身(入场后最高) 中位数: {bodies[len(bodies)//2]:+.1f}%")
    print(f"  鱼头/鱼身 中位数:      {ratios[len(ratios)//2]:.0f}%")
    
    return results
```

**关键判断**：
- 鱼头/鱼身 < 10% → 策略只吃鱼头，是高频轮动型
- 鱼头/鱼身 30-70% → 吃到了大部分鱼身，趋势跟踪型
- 鱼头/鱼身 > 70% → 几乎卖在最高点，理想状态

**注意**：占比低不一定是坏事。如果策略 PF 高、回撤低，说明"吃鱼头+高频轮动"本身就是有效模式。**不要因为鱼头占比低就盲目加长持有期。**

---

## 第 5 步：入场时机拆解（⭐新增）

判断入场信号是在趋势早期还是追高。

```python
def entry_timing(data, data_dir='data/etf'):
    """拆分波段：入场前 / 持仓中 / 卖出后 各占多少"""
    # ... 加载日线同第3步 ...
    
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    
    results = []
    for t in trades:
        code = t['code']
        bars = all_bars.get(code, {})
        if not bars: continue
        dates = sorted(bars.keys())
        buy_d, sell_d = t['buy_date'], t['sell_date']
        if buy_d not in dates or sell_d not in dates: continue
        
        buy_idx = dates.index(buy_d)
        sell_idx = dates.index(sell_d)
        buy_price = t['buy_price']
        sell_price = t['sell_price']
        
        # 买入前60日最低
        pre_start = max(0, buy_idx - 60)
        pre_low = min(bars[dates[i]] for i in range(pre_start, buy_idx + 1))
        
        # 卖出后6月最高
        post_end = min(len(dates) - 1, sell_idx + 126)
        post_high = max(bars[dates[i]] for i in range(sell_idx, post_end + 1))
        
        pre_entry = (buy_price / pre_low - 1) * 100       # 入场前已涨
        during = t['profit_pct']                           # 持仓期间
        post_sell = (post_high / sell_price - 1) * 100    # 卖出后
        
        full_swing = (post_high / pre_low - 1) * 100
        
        results.append({
            'pre_entry': pre_entry, 'during': during,
            'post_sell': post_sell, 'full_swing': full_swing,
        })
    
    valid = [r for r in results if r['full_swing'] > 0]
    pre = sorted([r['pre_entry'] for r in valid])
    dur = sorted([r['during'] for r in valid])
    post = sorted([r['post_sell'] for r in valid])
    swing = sorted([r['full_swing'] for r in valid])
    
    print(f"\n  N={len(valid)}")
    print(f"  完整波段:  {swing[len(swing)//2]:+.1f}%")
    print(f"  入场前已涨: {pre[len(pre)//2]:+.1f}%  ({pre[len(pre)//2]/swing[len(swing)//2]*100:.0f}%)")
    print(f"  持仓期间:  {dur[len(dur)//2]:+.1f}%  ({dur[len(dur)//2]/swing[len(swing)//2]*100:.0f}%)")
    print(f"  卖出后继续: {post[len(post)//2]:+.1f}%  ({post[len(post)//2]/swing[len(swing)//2]*100:.0f}%)")
    
    # 入场早 vs 晚
    early = sum(1 for r in valid if r['pre_entry'] < r['full_swing'] * 0.3)
    late = sum(1 for r in valid if r['pre_entry'] > r['full_swing'] * 0.7)
    print(f"\n  早期入场(<30%波段完成): {early} ({early/len(valid)*100:.0f}%)")
    print(f"  晚期入场(>70%波段完成): {late} ({late/len(valid)*100:.0f}%)")
    
    return results
```

**关键判断**：
- 早期入场 > 50% → 选股信号有效
- 晚期入场 > 30% → 信号追高，入场逻辑有问题
- 持仓期间占比 < 10% 但卖出后占比 > 50% → 入场没问题，纯卖出问题

---

## 第 6 步：根因诊断 + 修复

基于前面 5 步的数据，判断策略的核心问题：

| 症状 | 根因 | 方向 |
|------|------|------|
| 交易数=0 | gate 或信号逻辑有 bug | 检查 market_gate、generate_signals 时序 |
| 某类卖出后 median 1月 > +5% | 卖出太紧太敏感 | 放宽/确认机制 |
| 入场时机好(>50%早期) 但 鱼头/鱼身 < 10% | **策略基因就是高频轮动** | 不改持有期，提仓位/放信号量 |
| PF > 2.5 但改卖出变差 | 买入卖出是配套逻辑 | 不要拆开，微调参数而非换逻辑 |
| 胜率太低(<30%) | 买入时机不对 | 加确认条件 |

### ⭐ 最重要的教训

**如果前向分析说卖早了，但原版 PF > 2.5，95% 的情况下不要改卖出逻辑。** 原因是：

1. 买入信号和卖出信号往往是同源的（如 corr 买入 + corr 卖出），拆开会破坏逻辑闭环
2. 前向分析只看单品种，不计算仓位机会成本
3. 高频吃鱼头 + 复利 可能比低频吃鱼身收益更高
4. 趋势卖出（MA20/MA60）对于短周期信号是灾难

### 修复优先级

1. **先改参数不换逻辑** — 如放宽 CORR_BUY_MAX 让信号更多、提高 MAX_POSITIONS
2. **换卖出逻辑成功率很低** — 买入卖出是一对，拆开大概率变差
3. **如果改卖出，先跑完整回测再对比** — 不要只看前向分析就下结论

---

## 实战案例：etf_alpha042 的完整分析

```
Step 1 概览: +127.80%, PF 3.41, 回撤 16.58% → 策略优秀

Step 2 卖出拆解:
  量价同步(corr>0.5): 139笔(90%), 胜率 67%
  到期:               16笔(10%), 胜率 56%
  → corr>0.5 卖出是主力，胜率不差

Step 3 前向追踪:
  量价同步组: 1月后 median -0.8%, 6月后 +6.9%, 81% 创新高
  → 短期卖对了(1月跌)，中长期卖早了(6月涨)

Step 4 鱼头鱼身:
  鱼头中位数 +1.4%, 鱼身 +15.6%, 占比 9%
  → 只吃鱼头

Step 5 入场时机:
  53% 早期入场, 入场前已涨 +7.1%
  → 入场信号没问题

Step 6 尝试修复:
  V2(二次确认): +59%, PF 腰斩     → 反向
  V3(MA20趋势): +41%, PF 1.61    → 反向  
  V4(MA60趋势): -7.3%, PF 0.92   → 崩了
  
结论: 原版 V1 就是最优。这个策略的竞争力不在单笔赚多少，
     而在周转速度 — 用仓位轮动复利替代单笔暴利。
     不要因为"前向分析说卖早了"就去改卖出。
```

---

## 不允许做的事

- **不跑多策略对比**（那是 battle 接口的事）
- **不修改数据库**
- **不在没诊断清楚前盲目改参数** — 先看数据，再动手
- **不要因为前向分析"卖早了"就急着改卖出逻辑** — 组合层面回测是唯一标准
- **不要用个股数据目录分析 ETF 策略，反之亦然** — 数据源要匹配
